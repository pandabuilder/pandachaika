import json
import logging
import re
from functools import reduce
from random import randint

import operator
from typing import Any, Optional, Union

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.models import User
from django.http.request import HttpRequest
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.urls import reverse
from django.db.models import Q, Avg, Max, Min, Sum, Count, QuerySet, F, Prefetch
from django.http import Http404, BadHeaderError
from django.http import HttpResponseRedirect, HttpResponse
from django.http.request import QueryDict
from django.shortcuts import redirect, render
from django.conf import settings
from django.utils.html import urlize, linebreaks

from core.base.setup import Settings
from core.base.types import DataDict
from viewer.utils.actions import event_log

from viewer.forms import (
    ArchiveSearchForm,
    GallerySearchForm,
    SpanErrorList, ArchiveSearchSimpleForm, BootstrapPasswordChangeForm, ProfileChangeForm, UserChangeForm)
from viewer.models import (
    Archive, Image, Tag, Gallery,
    UserArchivePrefs, WantedGallery,
    users_with_perm, Profile, GalleryQuerySet, GallerySubmitEntry, ArchiveGroup, GalleryProviderData)
from viewer.utils.functions import send_mass_html_mail, gallery_search_results_to_json, archive_search_result_to_json
from viewer.utils.tags import sort_tags
from viewer.utils.types import AuthenticatedHttpRequest

logger = logging.getLogger(__name__)
crawler_settings = settings.CRAWLER_SETTINGS

gallery_filter_keys = (
    "title", "rating_from", "rating_to", "filesize_from",
    "filesize_to", "filecount_from", "filecount_to", "posted_from", "posted_to",
    "create_from", "create_to",
    "category", "provider", "dl_type",
    "expunged", "hidden", "fjord", "uploader", "tags", "not_used", "reason",
    "contains", "contained", "not_normal", "crc32", "used"
)

archive_filter_keys = (
    "title", "filename", "filecount_from", "filecount_to",
    "filesize_from", "filesize_to",
    "rating_from", "rating_to", "match_type", "posted_from",
    "posted_to", "source_type", "tags", "only_favorites",
    "non_public", "public", "reason",
    "uploader", "category", "hidden",
    "qsearch", "extracted"
)

archive_order_fields = [
    "title", "title_jpn", "rating", "filesize",
    "filecount", "posted", "create_date", "public_date", "last_modified",
    "category", "reason", "source_type", "gallery__posted", "gallery__uploader", "gallery__category"
]

archive_public_order_fields = [
    "title", "title_jpn", "rating", "filesize",
    "filecount", "posted", "public_date",
    "category", "reason", "source_type", "gallery__posted", "gallery__uploader", "gallery__category"
]

archive_sort_by_fields = [
    "title", "title_jpn", "filesize", "filecount", "public_date", "reason", "source_type",
    "create_date", "last_modified",
    "gallery__category", "gallery__posted", "gallery__uploader", "gallery__category"
]

archive_public_sort_by_fields = [
    "title", "title_jpn", "filesize", "filecount", "public_date", "reason", "source_type",
    "gallery__category", "gallery__posted", "gallery__uploader", "gallery__category"
]

gallery_order_fields = (
    "title", "title_jpn", "rating", "filesize",
    "filecount", "posted", "create_date",
    "category", "provider", "uploader"
)

wanted_gallery_filter_keys = (
    "title", "wanted_page_count_lower", "wanted_page_count_upper",
    "provider", "not_used", "wanted-should-search", "wanted-should-search-not", "book_type",
    "publisher", "wanted-found", "wanted-not-found", "reason",
    "wanted-no-found-galleries", "with-possible-matches", "tags",
    "mention-source"
)

wanted_gallery_order_fields = (
    "title", "date_found",
    "create_date", "last_modified",
    "release_date"
)


def viewer_login(request: HttpRequest) -> HttpResponse:

    if request.method == 'POST':
        username = request.POST['username']
        password = request.POST['password']
        user = authenticate(username=username, password=password)
        if user is not None:
            if user.is_active:
                login(request, user)
                next_url = request.POST.get('next', 'viewer:main-page')
                return redirect(next_url)
            else:
                return render_error(request, "This account has been disabled.")
        else:
            return render_error(request, "Invalid login credentials.")
    else:
        next_url = request.GET.get('next', 'viewer:main-page')
        d = {'next': next_url}
        return render(request, 'viewer/accounts/login.html', d)


@login_required
def viewer_logout(request: HttpRequest) -> HttpResponse:
    logout(request)
    return HttpResponseRedirect(reverse('viewer:main-page'))


@login_required
def change_password(request: HttpRequest) -> HttpResponse:
    if request.method == 'POST':
        form = BootstrapPasswordChangeForm(request.user, request.POST, error_class=SpanErrorList)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user) 
            messages.success(request, 'Your password was successfully updated!')
            return redirect('viewer:change-password')
        else:
            messages.error(request, 'Please correct the error below.')
    else:
        form = BootstrapPasswordChangeForm(request.user, error_class=SpanErrorList)
    return render(request, 'viewer/accounts/change_password.html', {
        'form': form
    })


@login_required
def change_profile(request: AuthenticatedHttpRequest) -> HttpResponse:
    if not hasattr(request.user, 'profile'):
        Profile.objects.create(user=request.user)
    if request.method == 'POST':
        user_form = UserChangeForm(request.POST, instance=request.user, error_class=SpanErrorList)
        profile_form = ProfileChangeForm(request.POST, instance=request.user.profile, error_class=SpanErrorList)
        if all((user_form.is_valid(), profile_form.is_valid())):
            user = user_form.save()
            profile = profile_form.save(commit=False)
            profile.user = user
            profile.save()
            messages.success(request, 'Your profile was successfully updated!')
            return redirect('viewer:change-profile')
        else:
            messages.error(request, 'Please correct the error below.')
    else:
        user_form = UserChangeForm(instance=request.user, error_class=SpanErrorList)
        profile_form = ProfileChangeForm(instance=request.user.profile, error_class=SpanErrorList)
    return render(request, 'viewer/accounts/change_profile.html', {
        'profile_form': profile_form,
        'user_form': user_form
    })


def session_settings(request: HttpRequest) -> HttpResponse:
    if request.method == 'POST':
        data = json.loads(request.body.decode("utf-8"))
        if 'viewer_parameters' in data:
            if "viewer_parameters" not in request.session:
                request.session["viewer_parameters"] = {}
            for k, v in data.items():
                if not k == 'viewer_parameters':
                    request.session["viewer_parameters"][k] = v
            request.session.modified = True
        return HttpResponse(json.dumps({'result': "ok"}), content_type="application/json; charset=utf-8")
    elif request.method == 'GET':
        data = json.loads(request.body.decode("utf-8"))
        if 'viewer_parameters' in data:
            if "viewer_parameters" not in request.session:
                request.session["viewer_parameters"] = {}
                request.session["viewer_parameters"]["image_width"] = 900
                request.session.modified = True
            return HttpResponse(
                json.dumps(request.session["viewer_parameters"]), content_type="application/json; charset=utf-8"
            )
        return HttpResponse(json.dumps({'result': "error"}), content_type="application/json; charset=utf-8")
    else:
        return HttpResponse(json.dumps({'result': "error"}), content_type="application/json; charset=utf-8")


# TODO: Generalize this script for several providers.
# Since we expose the api key here, until this is a per user parameter, only an admin can use this.
@staff_member_required(login_url='viewer:login')  # type: ignore
def panda_userscript(request: HttpRequest) -> HttpResponse:

    return render(
        request,
        'viewer/panda.user.js',
        {
            "api_key": crawler_settings.api_key,
            "img_url": request.build_absolute_uri(crawler_settings.urls.static_url + 'favicon-160.png'),
            "server_url": request.build_absolute_uri(reverse('viewer:json-parser'))
        },
        content_type='application/javascript'
    )


def image_url(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        image = Image.objects.get(id=pk, extracted=True)
    except Image.DoesNotExist:
        raise Http404("Image does not exist")
    if not image.archive.public and not request.user.is_authenticated:
        raise Http404("Image is not public")
    if 'HTTP_X_FORWARDED_HOST' in request.META:
        response = HttpResponse()
        # response["Content-Disposition"] = 'attachment; filename="{0}"'.format(
        #         archive.pretty_name)
        response['Content-Type'] = ""
        response['X-Accel-Redirect'] = "/image/{0}".format(image.image.name)
        return response
    else:
        return HttpResponseRedirect(image.image.url)


def gallery_details(request: HttpRequest, pk: int, tool: Optional[str] = None) -> HttpResponse:
    try:
        gallery = Gallery.objects.get(pk=pk)
    except Gallery.DoesNotExist:
        raise Http404("Gallery does not exist")
    if not (gallery.public or request.user.is_authenticated):
        raise Http404("Gallery does not exist")

    if request.user.has_perm('viewer.download_gallery') and tool == "download":
        if 'downloader' in request.GET and request.user.is_staff:
            current_settings = Settings(load_from_config=crawler_settings.config)
            current_settings.allow_downloaders_only([request.GET['downloader']], True, True, True)
            if current_settings.workers.web_queue:
                current_settings.workers.web_queue.enqueue_args_list((gallery.get_link(),), override_options=current_settings)
        else:
            # Since this is used from the gallery page mainly to download an already added gallery using
            # downloader settings, force replace_metadata and retry_failed
            current_settings = Settings(load_from_config=crawler_settings.config)
            current_settings.replace_metadata = True
            current_settings.retry_failed = True
            if current_settings.workers.web_queue:
                current_settings.workers.web_queue.enqueue_args_list((gallery.get_link(),), override_options=current_settings)
        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    if request.user.is_staff and tool == "toggle-hidden":
        gallery.hidden = not gallery.hidden
        gallery.save()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    if request.user.has_perm('viewer.publish_gallery') and tool == "toggle-public":
        gallery.public_toggle()

        if gallery.public:
            event_log(
                request.user,
                'PUBLISH_GALLERY',
                content_object=gallery,
                result='success'
            )
        else:
            event_log(
                request.user,
                'UNPUBLISH_GALLERY',
                content_object=gallery,
                result='success'
            )

        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    if request.user.has_perm('viewer.mark_delete_gallery') and tool == "mark-deleted":
        gallery.mark_as_deleted()

        event_log(
            request.user,
            'MARK_DELETE_GALLERY',
            content_object=gallery,
            result='deleted'
        )

        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    if request.user.has_perm('viewer.mark_delete_gallery') and tool == "mark-normal":
        gallery.mark_as_normal()

        event_log(
            request.user,
            'MARK_NORMAL_GALLERY',
            content_object=gallery,
            result='success'
        )

        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    if request.user.has_perm('viewer.update_metadata') and tool == "recall-api":

        current_settings = Settings(load_from_config=crawler_settings.config)

        if current_settings.workers.web_queue and gallery.provider:

            def gallery_callback(x: Optional['Gallery'], crawled_url: Optional[str], result: str) -> None:
                event_log(
                    request.user,
                    'UPDATE_METADATA',
                    content_object=x,
                    result=result,
                    data=crawled_url
                )

            current_settings.set_update_metadata_options(providers=(gallery.provider,))

            current_settings.workers.web_queue.enqueue_args_list(
                (gallery.get_link(),),
                override_options=current_settings,
                gallery_callback=gallery_callback
            )

            logger.info(
                'Updating gallery API data for gallery: {} and related archives'.format(
                    gallery.get_absolute_url()
                )
            )

        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    if request.user.has_perm('viewer.update_metadata') and tool == "refetch-thumbnail":

        if gallery.thumbnail_url:

            success = gallery.fetch_thumbnail(force_redownload=True)

            event_log(
                request.user,
                'REFETCH_THUMBNAIL',
                content_object=gallery,
                result='sucess' if success else 'failed',
                data=gallery.thumbnail_url
            )

            logger.info(
                'Refetching gallery: {} thumbnail from url: {}'.format(
                    gallery.get_absolute_url(), gallery.thumbnail_url
                )
            )

        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    tag_lists = sort_tags(gallery.tags.all())

    gallery_provider_data = GalleryProviderData.objects.filter(gallery=gallery)

    d = {
        'gallery': gallery, 'tag_lists': tag_lists, 'settings': crawler_settings,
        'gallery_provider_data': gallery_provider_data,
    }

    if gallery.first_gallery:
        gallery_filters = Q(first_gallery=gallery.first_gallery) | Q(first_gallery=gallery) | Q(pk=gallery.pk) | Q(pk=gallery.first_gallery.pk)
    else:
        gallery_filters = Q(first_gallery=gallery) | Q(pk=gallery.pk)

    d.update({'gallery_chain': Gallery.objects.filter(gallery_filters, provider=gallery.provider).order_by('gid')})

    return render(request, "viewer/gallery.html", d)


@login_required
def gallery_enter_reason(request: HttpRequest, pk: int, tool: Optional[str] = None) -> HttpResponse:
    try:
        gallery = Gallery.objects.get(pk=pk)
    except Gallery.DoesNotExist:
        raise Http404("Gallery does not exist")
    if not request.user.is_authenticated:
        raise Http404("Gallery does not exist")

    if request.method == 'POST':

        p = request.POST
        user_reason = p.get('reason', '')
        if "confirm_tool" in p:

            if request.user.has_perm('viewer.download_gallery') and tool == "download":
                if 'downloader' in request.GET and request.user.is_staff:
                    current_settings = Settings(load_from_config=crawler_settings.config)
                    current_settings.allow_downloaders_only([request.GET['downloader']], True, True, True)
                    if current_settings.workers.web_queue:
                        current_settings.workers.web_queue.enqueue_args_list((gallery.get_link(),), override_options=current_settings)
                else:
                    # Since this is used from the gallery page mainly to download an already added gallery using
                    # downloader settings, force replace_metadata and retry_failed
                    current_settings = Settings(load_from_config=crawler_settings.config)
                    current_settings.replace_metadata = True
                    current_settings.retry_failed = True
                    if current_settings.workers.web_queue:
                        current_settings.workers.web_queue.enqueue_args_list((gallery.get_link(),), override_options=current_settings)
                return HttpResponseRedirect(gallery.get_absolute_url())

            if request.user.is_staff and tool == "toggle-hidden":
                gallery.hidden = not gallery.hidden
                gallery.save()
                return HttpResponseRedirect(gallery.get_absolute_url())

            if request.user.has_perm('viewer.publish_gallery') and tool == "toggle-public":
                gallery.public_toggle()

                if gallery.public:
                    event_log(
                        request.user,
                        'PUBLISH_GALLERY',
                        content_object=gallery,
                        result='success',
                        reason=user_reason
                    )
                else:
                    event_log(
                        request.user,
                        'UNPUBLISH_GALLERY',
                        content_object=gallery,
                        result='success',
                        reason=user_reason
                    )

                return HttpResponseRedirect(gallery.get_absolute_url())

            if request.user.has_perm('viewer.mark_delete_gallery') and tool == "mark-deleted":
                gallery.mark_as_deleted()

                event_log(
                    request.user,
                    'MARK_DELETE_GALLERY',
                    content_object=gallery,
                    result='deleted',
                    reason=user_reason
                )

                message = "Gallery: {} will be marked as deleted, reason: {}".format(gallery.get_absolute_url(), user_reason)

                logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)

                return HttpResponseRedirect(gallery.get_absolute_url())

            if request.user.has_perm('viewer.mark_delete_gallery') and tool == "mark-normal":
                gallery.mark_as_normal()

                event_log(
                    request.user,
                    'MARK_NORMAL_GALLERY',
                    content_object=gallery,
                    result='success',
                    reason=user_reason
                )

                message = "Gallery: {} will be marked as normal, reason: {}".format(gallery.get_absolute_url(), user_reason)

                logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)

                return HttpResponseRedirect(gallery.get_absolute_url())

            if request.user.has_perm('viewer.update_metadata') and tool == "recall-api":

                current_settings = Settings(load_from_config=crawler_settings.config)

                if current_settings.workers.web_queue and gallery.provider:

                    def gallery_callback(x: Optional['Gallery'], crawled_url: Optional[str], result: str) -> None:
                        event_log(
                            request.user,
                            'UPDATE_METADATA',
                            content_object=x,
                            result=result,
                            data=crawled_url,
                            reason=user_reason
                        )

                    current_settings.set_update_metadata_options(providers=(gallery.provider,))

                    current_settings.workers.web_queue.enqueue_args_list(
                        (gallery.get_link(),),
                        override_options=current_settings,
                        gallery_callback=gallery_callback
                    )

                    logger.info(
                        'Updating gallery API data for gallery: {} and related archives'.format(
                            gallery.get_absolute_url()
                        )
                    )

                return HttpResponseRedirect(gallery.get_absolute_url())

            if request.user.has_perm('viewer.update_metadata') and tool == "refetch-thumbnail":

                if gallery.thumbnail_url:

                    success = gallery.fetch_thumbnail(force_redownload=True)

                    event_log(
                        request.user,
                        'REFETCH_THUMBNAIL',
                        content_object=gallery,
                        result='sucess' if success else 'failed',
                        data=gallery.thumbnail_url,
                        reason=user_reason
                    )

                    logger.info(
                        'Refetching gallery: {} thumbnail from url: {}'.format(
                            gallery.get_absolute_url(), gallery.thumbnail_url
                        )
                    )

                return HttpResponseRedirect(gallery.get_absolute_url())

    d = {'gallery': gallery, 'tool': tool}

    inlined = request.GET.get("inline", None)

    if inlined:
        return render(request, "viewer/include/modals/gallery_tool_reason.html", d)
    else:
        return render(request, "viewer/gallery_display_tool.html", d)


def gallery_thumb(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        gallery = Gallery.objects.get(pk=pk)
    except Gallery.DoesNotExist:
        raise Http404("Gallery does not exist")
    if not gallery.public and not request.user.is_authenticated:
        raise Http404("Gallery is not public")
    if 'HTTP_X_FORWARDED_HOST' in request.META:
        response = HttpResponse()
        response["Content-Type"] = "image/jpeg"
        # response["Content-Disposition"] = 'attachment; filename*=UTF-8\'\'{0}'.format(
        #         archive.pretty_name)
        response['X-Accel-Redirect'] = "/image/{0}".format(gallery.thumbnail.name)
        return response
    else:
        return HttpResponseRedirect(gallery.thumbnail.url)


def gallery_list(request: HttpRequest, mode: str = 'none', tag: Optional[str] = None) -> HttpResponse:
    """Search, filter, sort galleries."""
    try:
        page = int(request.GET.get("page", '1'))
    except ValueError:
        page = 1

    cleared_queries = request.GET.copy()
    queries_to_clear = ("page", "view", "apply")

    for entry in queries_to_clear:
        if entry in cleared_queries:
            del cleared_queries[entry]

    get = request.GET
    request.GET = cleared_queries  # type: ignore

    json_request = 'json' in get

    display_prms: dict[str, Any] = {}

    # init parameters
    if "gallery_parameters" in request.session:
        parameters = request.session["gallery_parameters"]
    else:
        parameters = {}

    keys = ("sort", "asc_desc")

    for k in keys:
        if k not in parameters:
            parameters[k] = ''

    for k in gallery_filter_keys:
        if k not in display_prms:
            display_prms[k] = ''

    parameters['show_source'] = False

    if "clear" in get:
        for k in gallery_filter_keys:
            display_prms[k] = ''
        request.GET = QueryDict('')
        # request.session["gallery_parameters"].clear()
    else:
        if mode == 'gallery-tag' and tag:
            display_prms['tags'] = "^" + tag
        else:
            # create dictionary of properties for each archive and a dict of
            # search/filter parameters
            for k, v in get.items():
                if k in parameters:
                    parameters[k] = v
                elif k in display_prms:
                    display_prms[k] = v
    # Fill default parameters
    if 'view' not in parameters or parameters['view'] == '':
        parameters['view'] = 'list'
    if 'asc_desc' not in parameters or parameters['asc_desc'] == '':
        parameters['asc_desc'] = 'desc'
    if 'sort' not in parameters or parameters['sort'] == '':
        parameters['sort'] = 'posted'

    parameters['main_filters'] = True
    request.session["gallery_parameters"] = parameters
    results = filter_galleries(request, parameters, display_prms, json_request=json_request)

    if json_request:
        if request.user.is_authenticated:
            used_prefetch = Prefetch('archive_set', to_attr='available_archives')
        else:
            used_prefetch = Prefetch('archive_set', queryset=Archive.objects.filter(public=True), to_attr='available_archives')
        results = results.prefetch_related(
            'tags',
            used_prefetch
        ).distinct()

    if "random" in get:
        count = results.count()
        if count > 0:
            random_index = randint(0, count - 1)
            return redirect(results[random_index])

    # make paginator
    galleries_per_page = 100

    paginator = Paginator(results, galleries_per_page)
    try:
        results_page = paginator.page(page)

    except (InvalidPage, EmptyPage):
        results_page = paginator.page(1)

    if json_request:
        response = json.dumps(
            {
                'galleries': gallery_search_results_to_json(request, results_page),
                'has_previous': results_page.has_previous(),
                'has_next': results_page.has_next(),
                'num_pages': paginator.num_pages,
                'count': paginator.count,
                'number': results_page.number,
            },
            # indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        return HttpResponse(response, content_type="application/json; charset=utf-8")

    display_prms['page_range'] = list(
        range(
            max(1, results_page.number - 3 - max(0, results_page.number - (paginator.num_pages - 3))),
            min(paginator.num_pages + 1, results_page.number + 3 + 1 - min(0, results_page.number - 3 - 1))
        )
    )

    form = GallerySearchForm(initial={'title': display_prms['title'],
                                      'tags': display_prms['tags']})

    d = {
        'results': results_page, 'prm': parameters,
        'display_prms': display_prms, 'form': form
    }

    return render(request, "viewer/gallery_search.html", d)


filter_galleries_defer = (
    "origin", "status", "thumbnail_url", "dl_type", "public", "fjord",
    "hidden", "rating", "expunged", "comment", "gallery_container_id", "magazine_id", "uploader",
    "reason", "last_modified"
)


def filter_galleries(request: HttpRequest, session_filters: dict[str, str], request_filters: dict[str, str], json_request: bool = False) -> Union[GalleryQuerySet, QuerySet[Gallery]]:
    """Filter gallery results through parameters and return results list."""

    # sort and filter results by parameters
    order = "posted"
    if session_filters["sort"]:
        order = session_filters["sort"]
    if session_filters["asc_desc"] == "desc":
        results: Union[GalleryQuerySet, QuerySet[Gallery]] = Gallery.objects.order_by(F(order).desc(nulls_last=True))
    else:
        results = Gallery.objects.order_by(F(order).asc(nulls_last=True))

    if not request.user.is_authenticated:
        results = results.filter(public=True)

    if request_filters["title"]:
        q_formatted = '%' + request_filters["title"].replace(' ', '%') + '%'
        results = results.filter(
            Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
        )
    if request_filters["rating_from"]:
        results = results.filter(rating__gte=float(request_filters["rating_from"]))
    if request_filters["rating_to"]:
        results = results.filter(rating__lte=float(request_filters["rating_to"]))
    if request_filters["filecount_from"]:
        results = results.filter(filecount__gte=int(float(request_filters["filecount_from"])))
    if request_filters["filecount_to"]:
        results = results.filter(filecount__lte=int(float(request_filters["filecount_to"])))
    if request_filters["filesize_from"]:
        results = results.filter(filesize__gte=float(request_filters["filesize_from"]))
    if request_filters["filesize_to"]:
        results = results.filter(filesize__lte=float(request_filters["filesize_to"]))
    if request_filters["category"]:
        results = results.filter(category__icontains=request_filters["category"])
    if request_filters["expunged"]:
        results = results.filter(expunged=request_filters["expunged"])
    if request_filters["fjord"]:
        results = results.filter(fjord=request_filters["fjord"])
    if request_filters["uploader"]:
        results = results.filter(uploader=request_filters["uploader"])
    if request_filters["provider"]:
        results = results.filter(provider=request_filters["provider"])

    if request_filters["contained"]:
        results = results.filter(Q(gallery_container__isnull=False) | Q(magazine__isnull=False))
    if request_filters["contains"]:
        results = results.annotate(
            num_contains=Count('gallery_contains'), num_chapters=Count('magazine_chapters')
        ).filter(Q(num_contains__gt=0) | Q(num_chapters__gt=0))
    if request_filters["reason"]:
        results = results.filter(reason__contains=request_filters["reason"])
    if request.user.is_authenticated:
        if request_filters["not_used"]:
            results = results.non_used_galleries()  # type: ignore
        if request_filters["hidden"]:
            results = results.filter(hidden=request_filters["hidden"])
        if "not_normal" not in request_filters or not request_filters["not_normal"]:
            results = results.eligible_for_use()  # type: ignore
    else:
        results = results.eligible_for_use()  # type: ignore

    if request_filters["tags"]:
        tags = request_filters["tags"].split(',')
        for tag in tags:
            tag = tag.strip().replace(" ", "_")
            tag_clean = re.sub("^[-|^]", "", tag)
            scope_name = tag_clean.split(":", maxsplit=1)
            if len(scope_name) > 1:
                tag_scope = scope_name[0]
                tag_name = scope_name[1]
            else:
                tag_scope = ''
                tag_name = scope_name[0]
            if tag.startswith("-"):
                if tag_name != '' and tag_scope != '':
                    tag_query = Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)
                elif tag_name != '':
                    tag_query = Q(tags__name__contains=tag_name)
                else:
                    tag_query = Q(tags__scope__contains=tag_scope)

                results = results.exclude(
                    tag_query
                )
            elif tag.startswith("^"):
                if tag_name != '' and tag_scope != '':
                    tag_query = Q(tags__name__exact=tag_name) & Q(tags__scope__exact=tag_scope)
                elif tag_name != '':
                    tag_query = Q(tags__name__exact=tag_name)
                else:
                    tag_query = Q(tags__scope__exact=tag_scope)

                results = results.filter(
                    tag_query
                )
            else:
                if tag_name != '' and tag_scope != '':
                    tag_query = Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)
                elif tag_name != '':
                    tag_query = Q(tags__name__contains=tag_name)
                else:
                    tag_query = Q(tags__scope__contains=tag_scope)

                results = results.filter(
                    tag_query
                )

        results = results.distinct()

    # Remove fields that are admin related, not public facing
    if not json_request:
        results = results.defer(*filter_galleries_defer)

    return results


def search(request: HttpRequest, mode: str = 'none', tag: Optional[str] = None) -> HttpResponse:
    """Search, filter, sort archives."""
    try:
        page = int(request.GET.get("page", '1'))
    except ValueError:
        page = 1

    cleared_queries = request.GET.copy()

    queries_to_clear = ("view", "clear", "apply")
    view_options = ("cover", "extended", "list")

    for entry in queries_to_clear:
        if entry in cleared_queries:
            del cleared_queries[entry]

    get = request.GET

    json_request = 'json' in get

    if 'rss' in get:
        return redirect('viewer:archive-rss')

    request.GET = cleared_queries  # type: ignore

    display_prms: dict[str, Any] = {}

    if "parameters" in request.session:
        parameters = request.session["parameters"]
    else:
        parameters = {}

    keys = ("sort", "asc_desc")

    for k in keys:
        if k not in parameters:
            parameters[k] = ''

    for k in archive_filter_keys:
        if k not in display_prms:
            display_prms[k] = ''

    if "only_favorites" not in get:
        display_prms['only_favorites'] = 0
    if "clear" in get:
        for k in keys:
            display_prms[k] = ''
        request.GET = QueryDict('')
        form = ArchiveSearchForm(initial={'title': '', 'tags': ''})
        form_simple = ArchiveSearchSimpleForm(initial={'source_type': ''})
    else:
        form_simple = ArchiveSearchSimpleForm(request.GET, error_class=SpanErrorList)
        form_simple.is_valid()
        for field_name, errors in form_simple.errors.items():
            messages.error(request, field_name + ": " + ", ".join(errors), extra_tags='danger')
        if mode == 'tag' and tag:
            display_prms['tags'] = "^" + tag
        # create dictionary of properties for each archive and a dict of
        # search/filter parameters
        for k, v in get.items():
            if k in parameters:
                parameters[k] = v
            elif k in display_prms:
                if k in form_simple.fields:
                    if k in form_simple.cleaned_data:
                        display_prms[k] = v
                else:
                    display_prms[k] = v
        form = ArchiveSearchForm(initial={'title': display_prms['title'], 'tags': display_prms['tags']})
    if 'view' not in parameters or parameters['view'] == '':
        parameters['view'] = 'list'
    if 'asc_desc' not in parameters or parameters['asc_desc'] == '':
        parameters['asc_desc'] = 'desc'
    if 'sort' not in parameters or parameters['sort'] == '':
        if request.user.is_authenticated:
            parameters['sort'] = 'create_date'
        else:
            parameters['sort'] = 'public_date'
    if 'qsearch' in get:
        parameters['main_filters'] = False
        results = quick_search(request, parameters, display_prms)
    else:
        parameters['main_filters'] = True
        results = filter_archives(request, parameters, display_prms)

    request.session["parameters"] = parameters

    if "random" in get:
        count = results.count()
        if count > 0:
            random_index = randint(0, count - 1)
            return redirect(results[random_index])
    elif "gen-ddl" in get:
        links = [request.build_absolute_uri(reverse('viewer:archive-download', args=(x.pk,))) for x in results]
        return HttpResponse("\n".join(links), content_type="text/plain; charset=utf-8")

    if json_request:
        results = results.select_related('gallery'). \
            prefetch_related(
            'tags').defer('gallery__comment')

    # make paginator
    if parameters['view'] == 'list' or json_request:
        archives_per_page = 100
    else:
        archives_per_page = 24

    paginator = Paginator(results, archives_per_page)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(1)

    if json_request:
        response = json.dumps(
            {
                'archives': archive_search_result_to_json(request, results_page),
                'has_previous': results_page.has_previous(),
                'has_next': results_page.has_next(),
                'num_pages': paginator.num_pages,
                'count': paginator.count,
                'number': results_page.number,
            },
            # indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        return HttpResponse(response, content_type="application/json; charset=utf-8")

    display_prms['page_range'] = list(
        range(
            max(1, results_page.number - 3 - max(0, results_page.number - (paginator.num_pages - 3))),
            min(paginator.num_pages + 1, results_page.number + 3 + 1 - min(0, results_page.number - 3 - 1))
        )
    )

    d = {
        'results': results_page, 'extra_options': view_options,
        'prm': parameters, 'display_prms': display_prms,
        'form': form, 'form_simple': form_simple
    }
    return render(request, "viewer/archive_search.html", d)


def filter_archives(request: HttpRequest, session_filters: dict[str, str], request_filters: dict[str, str], force_private: bool = False) -> QuerySet[Archive]:
    """Filter results through parameters
    and return results list.
    """

    if not request.user.is_authenticated and not force_private:
        results: QuerySet[Archive] = Archive.objects.filter(public=True)
        to_use_order_fields = archive_public_order_fields
    else:
        results = Archive.objects.all()
        to_use_order_fields = archive_order_fields

    results = results.filter(binned=False)

    # sort and filter results by parameters
    order = "posted"
    sorting_field = order
    if session_filters["sort"] and session_filters["sort"] in to_use_order_fields:
        order = session_filters["sort"]
        sorting_field = order
    if order == 'rating':
        order = 'gallery__' + order
    elif order == 'posted':
        order = 'gallery__' + order
    elif order == 'category':
        order = 'gallery__' + order

    if session_filters["asc_desc"] == "desc":
        results = results.order_by(F(order).desc(nulls_last=True))
    else:
        results = results.order_by(F(order).asc(nulls_last=True))

    if request_filters["title"]:
        q_formatted = '%' + request_filters["title"].replace(' ', '%') + '%'
        results = results.filter(
            Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
        )
    if request_filters["filename"]:
        results = results.filter(zipped__icontains=request_filters["filename"])
    if request_filters["rating_from"]:
        results = results.filter(gallery__rating__gte=float(request_filters["rating_from"]))
    if request_filters["rating_to"]:
        results = results.filter(gallery__rating__lte=float(request_filters["rating_to"]))
    if request_filters["filecount_from"]:
        results = results.filter(filecount__gte=int(float(request_filters["filecount_from"])))
    if request_filters["filecount_to"]:
        results = results.filter(filecount__lte=int(float(request_filters["filecount_to"])))
    if request_filters["filesize_from"]:
        results = results.filter(filesize__gte=int(float(request_filters["filesize_from"])))
    if request_filters["filesize_to"]:
        results = results.filter(filesize__lte=int(float(request_filters["filesize_to"])))
    if request_filters["posted_from"]:
        results = results.filter(gallery__posted__gte=request_filters["posted_from"])
    if request_filters["posted_to"]:
        results = results.filter(gallery__posted__lte=request_filters["posted_to"])
    if request_filters["match_type"]:
        results = results.filter(match_type__icontains=request_filters["match_type"])
    if request_filters["source_type"]:
        results = results.filter(source_type__icontains=request_filters["source_type"])
    if request_filters["reason"]:
        results = results.filter(reason__icontains=request_filters["reason"])
    if request_filters["uploader"]:
        results = results.filter(gallery__uploader__icontains=request_filters["uploader"])
    if request_filters["category"]:
        results = results.filter(gallery__category__icontains=request_filters["category"])

    if request_filters["tags"]:
        tags = request_filters["tags"].split(',')
        for tag in tags:
            tag = tag.strip().replace(" ", "_")
            tag_clean = re.sub("^[-|^]", "", tag)
            scope_name = tag_clean.split(":", maxsplit=1)
            if len(scope_name) > 1:
                tag_scope = scope_name[0]
                tag_name = scope_name[1]
            else:
                tag_scope = ''
                tag_name = scope_name[0]
            if tag.startswith("-"):
                if tag_name != '' and tag_scope != '':
                    tag_query = (
                        Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)
                    )
                elif tag_name != '':
                    tag_query = (
                        Q(tags__name__contains=tag_name)
                    )
                else:
                    tag_query = (
                        Q(tags__scope__contains=tag_scope)
                    )

                results = results.exclude(
                    tag_query
                )
            elif tag.startswith("^"):
                if tag_name != '' and tag_scope != '':
                    tag_query = (
                        Q(tags__name__exact=tag_name) & Q(tags__scope__exact=tag_scope)
                    )
                elif tag_name != '':
                    tag_query = (
                        Q(tags__name__exact=tag_name)
                    )
                else:
                    tag_query = (
                        Q(tags__scope__exact=tag_scope)
                    )

                results = results.filter(
                    tag_query
                )
            else:
                if tag_name != '' and tag_scope != '':
                    tag_query = (
                        Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)
                    )
                elif tag_name != '':
                    tag_query = (
                            Q(tags__name__contains=tag_name)
                    )
                else:
                    tag_query = (
                            Q(tags__scope__contains=tag_scope)
                    )

                results = results.filter(
                    tag_query
                )

        # Examples where distinct is needed:
        # Same name for a tag with 2 different scopes: artist:syukurin, group:syukurin
        results = results.distinct()

    if "only_favorites" in request_filters and request_filters["only_favorites"] and request.user.is_authenticated:
        user_arch_ids = UserArchivePrefs.objects.filter(
            user=request.user.id, favorite_group__gt=0).values_list('archive')
        results = results.filter(id__in=user_arch_ids)

    if "archive-group" in request_filters and request_filters["archive-group"]:
        try:
            archive_group_instance = ArchiveGroup.objects.get(title_slug=request_filters["archive-group"])
            if archive_group_instance.public or request.user.is_authenticated:
                results = results.filter(archive_groups=archive_group_instance)
                if "group-order" in request_filters and request_filters["group-order"]:
                    results = results.order_by("archivegroupentry")
        except ArchiveGroup.DoesNotExist:
            pass

    if "non_public" in request_filters and request_filters["non_public"]:
        results = results.filter(public=False)

    if "public" in request_filters and request_filters["public"]:
        results = results.filter(public=True)

    if session_filters["view"] == "list":
        if sorting_field in ('rating', 'posted', 'last_modified'):
            results = results.select_related('gallery')
    elif session_filters["view"] == "cover":
        pass
    elif session_filters["view"] == "extended":
        results = results.select_related('gallery')\
            .prefetch_related('archivetag_set__tag')\
            .defer('gallery__comment', 'gallery__provider_metadata')

    results = results.defer("details", "origin")

    return results


def quick_search(request: HttpRequest, parameters: DataDict, display_parameters: DataDict) -> QuerySet[Archive]:
    """Quick search of archives."""
    # sort and filter results by parameters
    order = "posted"
    if parameters["sort"]:
        order = parameters["sort"]
    if order == 'rating':
        order = 'gallery__' + order
    elif order == 'posted':
        order = 'gallery__' + order
    elif order == 'category':
        order = 'gallery__' + order

    if parameters["asc_desc"] == "desc":
        # order = '-' + order
        results: QuerySet[Archive] = Archive.objects.order_by(F(order).desc(nulls_last=True))
    else:
        results = Archive.objects.order_by(F(order).asc(nulls_last=True))

    if not request.user.is_authenticated:
        results = results.filter(public=True)

    results = results.filter(binned=False)

    # URL search
    url = display_parameters["qsearch"]

    parsers = crawler_settings.provider_context.get_parsers(crawler_settings)
    gallery_ids_providers = list()
    for parser in parsers:
        if parser.id_from_url_implemented():
            accepted_urls = parser.filter_accepted_urls((url,))
            gallery_ids_providers.extend([(parser.id_from_url(x), parser.name) for x in accepted_urls])

    if gallery_ids_providers:
        query = reduce(
            operator.or_,
            (Q(gallery__gid=gid, gallery__provider=provider) for gid, provider in gallery_ids_providers)
        )

        results_url: Optional[QuerySet[Archive]] = results.filter(query)
    else:
        results_url = None
        
    clean_up_qsearch = display_parameters["qsearch"].replace("	", " ")

    q_formatted = '%' + clean_up_qsearch.replace(' ', '%') + '%'
    results = results.filter(
        Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
    )

    if results_url:
        results = results | results_url
    else:
        results = results

    if "non_public" in display_parameters and display_parameters["non_public"]:
        results = results.filter(public=False)

    if "public" in display_parameters and display_parameters["public"]:
        results = results.filter(public=True)

    if parameters["view"] == "list":
        results = results.select_related('gallery')
    elif parameters["view"] == "cover":
        pass
    elif parameters["view"] == "extended":
        results = results.select_related('gallery').\
            prefetch_related('tags')

    results = results.defer("details")

    return results


# TODO: Expose this search somewhere.
def quick_gallery_search(request: HttpRequest) -> QuerySet[Gallery]:
    """Quick search of galleries."""
    # sort and filter results by parameters
    order = "posted"

    results: QuerySet[Gallery] = Gallery.objects.order_by(F(order).desc(nulls_last=True))

    if not request.user.is_authenticated:
        results = results.filter(public=True)

    qsearch = request.GET.get("qsearch", '')

    # URL search
    url = qsearch

    parsers = crawler_settings.provider_context.get_parsers(crawler_settings)
    gallery_ids_providers = list()
    for parser in parsers:
        if parser.id_from_url_implemented():
            accepted_urls = parser.filter_accepted_urls((url,))
            gallery_ids_providers.extend([(parser.id_from_url(x), parser.name) for x in accepted_urls])

    if gallery_ids_providers:
        query = reduce(
            operator.or_,
            (Q(gid=gid, provider=provider) for gid, provider in gallery_ids_providers)
        )

        results_url: Optional[QuerySet[Gallery]] = results.filter(query)
    else:
        results_url = None

    q_formatted = '%' + qsearch.replace(' ', '%') + '%'
    results = results.filter(
        Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
    )

    if results_url:
        results = results | results_url
    else:
        results = results

    results = results.distinct()

    results = results.defer(*filter_galleries_defer)

    return results


def url_submit(request: HttpRequest) -> HttpResponse:
    """Submit given URLs."""

    if not crawler_settings.urls.enable_public_submit:
        if not request.user.is_staff:
            raise Http404("Page not found")
        else:
            return render_error(request, "Page disabled by settings (urls: enable_public_submit).")

    p = request.POST

    d = {}

    if p and "submit" in p:
        current_settings = Settings(load_from_config=crawler_settings.config)
        if not current_settings.workers.web_queue:
            messages.error(request, 'Cannot submit link currently. Please contact an admin.')
            return HttpResponseRedirect(reverse('viewer:url-submit'))
        url_set = set()
        # create dictionary of properties for each url
        current_settings.back_up_downloaders = current_settings.downloaders.copy()
        current_settings.replace_metadata = False
        current_settings.config['allowed']['replace_metadata'] = 'no'
        for k, v in current_settings.config['downloaders'].items():
            current_settings.config['downloaders'][k] = str(-1)
            current_settings.downloaders[k] = -1
        current_settings.config['downloaders']['panda_submit'] = str(1)
        current_settings.config['downloaders']['nhentai_submit'] = str(1)
        current_settings.downloaders['panda_submit'] = 1
        current_settings.downloaders['nhentai_submit'] = 1
        for p_k, p_v in p.items():
            if isinstance(p_v, str) and p_k == "urls":
                url_list = p_v.split("\n")
                for item in url_list:
                    # Don't allow commands.
                    if not item.startswith('-'):
                        url_set.add(item.rstrip('\r'))
        urls = list(url_set)
        if not urls:
            messages.error(request, 'Submission is empty.')
            return HttpResponseRedirect(reverse('viewer:url-submit'))

        reason = ''
        if 'reason' in p and p['reason'] != '':
            reason = p['reason']
            # Force limit string length (dl_type field max_length)
            # current_settings.gallery_reason = reason[:200]

        # As a security check, only finally set urls that pass the accepted_urls
        parsers = crawler_settings.provider_context.get_parsers(crawler_settings)

        url_messages = []
        admin_messages = []

        # Sometimes people submit messages here.
        found_valid_urls: list[str] = []
        url_provider_gallery_tuple = []

        for parser in parsers:
            if parser.id_from_url_implemented():
                urls_filtered = parser.filter_accepted_urls(urls)
                found_valid_urls.extend(urls_filtered)
                for url_filtered in urls_filtered:
                    gid = parser.id_from_url(url_filtered)
                    if not gid:
                        continue
                    gallery = Gallery.objects.filter(gid=gid, provider=parser.name).first()
                    url_provider_gallery_tuple.append((url_filtered, gallery, parser.name, gid))
                    if not gallery:
                        url_messages.append('{}: New URL, will be added to the submit queue'.format(
                            url_filtered
                        ))
                        messages.success(
                            request,
                            'URL {} was not in the backup and will be added to the submit queue.'.format(url_filtered)
                        )
                        continue
                    if gallery.is_submitted():
                        if gallery.public:
                            messages.info(
                                request,
                                'URL {} already exists but a source file isn\'t available: {}.'.format(
                                    url_filtered,
                                    request.build_absolute_uri(gallery.get_absolute_url())
                                )
                            )
                            url_messages.append('{}: Already in submit queue but no Archive found, link: {}, reason: {}'.format(
                                url_filtered, request.build_absolute_uri(gallery.get_absolute_url()), gallery.reason)
                            )
                        else:
                            messages.info(
                                request,
                                'URL {} already exists in the backup and it\'s being reviewed.'.format(url_filtered)
                            )
                            url_messages.append('{}: Already in submit queue, link: {}, reason: {}'.format(
                                url_filtered, request.build_absolute_uri(gallery.get_absolute_url()), gallery.reason)
                            )
                    elif gallery.is_deleted():
                        messages.info(
                            request,
                            'URL {} was deleted from the backup.'.format(
                                url_filtered
                            )
                        )
                        url_messages.append(
                            '{}: Was marked as deleted: {}'.format(
                                url_filtered,
                                request.build_absolute_uri(gallery.get_absolute_url())
                            )
                        )
                    elif gallery.is_denied():
                        messages.info(
                            request,
                            'URL {} was submitted but denied.'.format(
                                url_filtered
                            )
                        )
                        url_messages.append(
                            '{}: Was submitted but denied: {}'.format(
                                url_filtered,
                                request.build_absolute_uri(gallery.get_absolute_url())
                            )
                        )
                    elif gallery.public:
                        messages.info(
                            request,
                            'URL {} already exists in the backup: {}'.format(
                                url_filtered,
                                request.build_absolute_uri(gallery.get_absolute_url())
                            )
                        )
                        url_messages.append(
                            '{}: Already present, is public: {}'.format(
                                url_filtered,
                                request.build_absolute_uri(gallery.get_absolute_url())
                            )
                        )
                    else:
                        messages.info(
                            request,
                            'URL {} already exists in the backup and it\'s being reviewed.'.format(url_filtered)
                        )
                        url_messages.append(
                            '{}: Already present, is not public: {}'.format(
                                url_filtered,
                                request.build_absolute_uri(gallery.get_absolute_url())
                            )
                        )

        extra_text = [x for x in urls if x not in found_valid_urls]

        gallery_submit_entries: dict[str, GallerySubmitEntry] = {}
        gallery_submit_entries_by_id: dict[tuple[str, str], GallerySubmitEntry] = {}

        for url_filtered, gallery, provider_name, gallery_id in url_provider_gallery_tuple:
            if not gallery:
                submit_entry = GallerySubmitEntry(
                    submit_url=url_filtered,
                    submit_reason=reason,
                    submit_extra="\n".join(extra_text)
                )
                submit_entry.save()
            else:
                submit_entry = GallerySubmitEntry(
                    gallery=gallery,
                    resolved_status=GallerySubmitEntry.RESOLVED_ALREADY_PRESENT,
                    submit_url=url_filtered,
                    submit_reason=reason,
                    submit_extra="\n".join(extra_text),
                )
                submit_entry.save()

            gallery_submit_entries[url_filtered] = submit_entry
            gallery_submit_entries_by_id[(gallery_id, provider_name)] = submit_entry

        no_commands_in_args_list: list[str] = list()
        for parser in parsers:
            if parser.id_from_url_implemented():
                no_commands_in_args_list.extend(parser.filter_accepted_urls(urls))

        def gallery_callback(x: Optional['Gallery'], crawled_url: Optional[str], result: str) -> None:
            # TODO: This needs work, if an ex link is changed to a e-h link, it's not found
            if crawled_url in gallery_submit_entries:
                gallery_submit_entries[crawled_url].submit_result = result
                gallery_submit_entries[crawled_url].save()
            # For some reason we're getting a int here, convert to str
            if x:
                gallery_gid = str(x.gid)
                if (gid, x.provider) in gallery_submit_entries_by_id:
                    gallery_submit_entries_by_id[(gallery_gid, x.provider)].gallery = x
                    gallery_submit_entries_by_id[(gallery_gid, x.provider)].save()

        current_settings.workers.web_queue.enqueue_args_list(
            no_commands_in_args_list,
            override_options=current_settings,
            gallery_callback=gallery_callback
        )

        if reason:
            admin_subject = 'New submission, reason: {}'.format(reason)
        else:
            admin_subject = 'New submission, no reason given'

        if url_messages:
            admin_messages.append('URL details:\n{}'.format("\n".join(url_messages)))

        if extra_text:
            admin_messages.append('Extra non-URL text:\n{}'.format("\n".join(extra_text)))

        admin_messages.append(
            '\nYou can check the current submit queue in: {}'.format(
                request.build_absolute_uri(reverse('viewer:submit-queue'))
            )
        )

        # if current_settings.mail_logging.enable:
        #     mail_admins(admin_subject, "\n".join(admin_messages), html_message=urlize("\n".join(admin_messages)))

        # Mail users
        users_to_mail = users_with_perm(
            'viewer',
            'approve_gallery',
            Q(email__isnull=False) | ~Q(email__exact=''),
            profile__notify_new_submissions=True
        )

        mails = users_to_mail.values_list('email', flat=True)

        try:
            logger.info('New submission: sending emails to enabled users.')
            # (subject, message, from_email, recipient_list)
            datatuples = tuple([(
                admin_subject,
                "\n".join(admin_messages),
                urlize(linebreaks("\n".join(admin_messages))),
                crawler_settings.mail_logging.from_,
                (mail,)
            ) for mail in mails])
            send_mass_html_mail(datatuples, fail_silently=True)
        except BadHeaderError:
            logger.error('Failed sending emails: Invalid header found.')

        logger.info("{}\n{}".format(admin_subject, "\n".join(admin_messages)))

        return HttpResponseRedirect(reverse('viewer:url-submit'))
    elif p and "check" in p:
        submit_text = p.get("urls")

        url_dict = dict()
        if submit_text:
            for item in submit_text.split("\n"):
                # Don't allow commands.
                if not item.startswith('-'):
                    url_dict[item.rstrip('\r')] = 1

        url_list = list(url_dict.keys())
        parsers = crawler_settings.provider_context.get_parsers(crawler_settings)

        url_gallery_tuple = []

        for parser in parsers:
            if parser.id_from_url_implemented():
                urls_filtered = parser.filter_accepted_urls(url_list)
                for url_filtered in urls_filtered:
                    gid = parser.id_from_url(url_filtered)
                    if not gid:
                        continue
                    # We force public galleries only here.
                    found_galleries = Gallery.objects.filter(
                        gid=gid, provider=parser.name
                    ).prefetch_related('archive_set', 'alternative_sources')

                    url_gallery_tuple.append((url_filtered, found_galleries.first()))

        d.update({'results': url_gallery_tuple})

    return render(request, "viewer/url_submit.html", d)


def public_stats(request: HttpRequest) -> HttpResponse:
    """Display public galleries and archives stats."""
    if not crawler_settings.urls.enable_public_stats:
        if not request.user.is_staff:
            raise Http404("Page not found")
        else:
            return render_error(request, "Page disabled by settings (urls: enable_public_stats).")

    stats_dict = {
        "n_archives": Archive.objects.filter(public=True).count(),
        "n_galleries": Gallery.objects.filter(public=True).count(),
        "archive": Archive.objects.filter(public=True).filter(filesize__gt=0).aggregate(
            Avg('filesize'), Max('filesize'), Min('filesize'), Sum('filesize'), Avg('filecount'), Sum('filecount')),
        "gallery": Gallery.objects.filter(public=True).filter(filesize__gt=0).aggregate(
            Avg('filesize'), Max('filesize'), Min('filesize'), Sum('filesize'), Avg('filecount'), Sum('filecount')),
        "n_tags": Tag.objects.filter(archive_tags__public=True).distinct().count(),
        "top_10_tags": Tag.objects.filter(archive_tags__public=True).distinct().annotate(
            num_archive=Count('archive_tags')).order_by('-num_archive')[:10],
        "top_10_artist_tags": Tag.objects.filter(scope='artist', archive_tags__public=True).distinct().annotate(
            num_archive=Count('archive_tags')).order_by('-num_archive')[:10]
    }

    # Per category
    categories = Gallery.objects.filter(public=True).values_list('category', flat=True).distinct()

    categories_dict = {}

    for category in categories:
        categories_dict[category] = {
            'n_galleries': Gallery.objects.filter(public=True, category=category).count(),
            'gallery': Gallery.objects.filter(public=True).filter(
                filesize__gt=0, category=category
            ).aggregate(
                Avg('filesize'), Max('filesize'), Min('filesize'), Sum('filesize'), Avg('filecount'), Sum('filecount')
            )
        }

    # Per language tag
    languages = Tag.objects.filter(
        scope='language'
    ).exclude(
        scope='language', name='translated'
    ).annotate(num_gallery=Count('gallery')).order_by('-num_gallery').values_list('name', flat=True).distinct()

    languages_dict = {}

    languages_dict['untranslated'] = {
        'n_galleries': Gallery.objects.filter(public=True).exclude(tags__scope='language').distinct().count(),
        'gallery': Gallery.objects.filter(public=True).filter(
            filesize__gt=0, tags__scope='language'
        ).distinct().aggregate(
            Avg('filesize'), Max('filesize'), Min('filesize'), Sum('filesize'), Avg('filecount'), Sum('filecount')
        )
    }

    for language in languages:
        languages_dict[language] = {
            'n_galleries': Gallery.objects.filter(public=True).filter(tags__scope='language', tags__name=language).distinct().count(),
            'gallery': Gallery.objects.filter(public=True).filter(
                filesize__gt=0, tags__scope='language', tags__name=language
            ).distinct().aggregate(
                Avg('filesize'), Max('filesize'), Min('filesize'), Sum('filesize'), Avg('filecount'), Sum('filecount')
            )
        }

    d = {'stats': stats_dict, 'gallery_categories': categories_dict, 'gallery_languages': languages_dict}

    return render(request, "viewer/public_stats.html", d)


@login_required
def user_archive_preferences(request: AuthenticatedHttpRequest, archive_pk: int, setting: str) -> HttpResponse:
    """Archive user favorite toggle."""
    try:
        Archive.objects.get(pk=archive_pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if setting == 'favorite':
        current_user_archive_preferences, created = UserArchivePrefs.objects.get_or_create(
            user=User.objects.get(pk=request.user.id),
            archive=Archive.objects.get(pk=archive_pk),
            defaults={'favorite_group': 1}
        )
        if not created:
            current_user_archive_preferences.favorite_group = 1
            current_user_archive_preferences.save()
    elif setting == 'unfavorite':
        current_user_archive_preferences, created = UserArchivePrefs.objects.get_or_create(
            user=User.objects.get(pk=request.user.id),
            archive=Archive.objects.get(pk=archive_pk),
            defaults={'favorite_group': 0}
        )
        if not created:
            current_user_archive_preferences.favorite_group = 0
            current_user_archive_preferences.save()
    else:
        return render_error(request, "Unknown user preference.")
    return HttpResponseRedirect(request.META["HTTP_REFERER"],
                                {'user_archive_preferences': current_user_archive_preferences})


def filter_archives_simple(params: dict[str, Any], authenticated=False, show_binned=False) -> QuerySet[Archive]:
    """Filter results through parameters
    and return results list.
    """
    if 'sort_by' in params and params['sort_by']:
        if authenticated:
            to_use_order_fields = archive_sort_by_fields
        else:
            to_use_order_fields = archive_public_sort_by_fields
        try:
            sort_objs = json.loads(params['sort_by'])
        except ValueError:
            sort_objs = [{'id': 'posted', 'desc': True}]
        order_arguments = [
            F(x['id'].replace(".", "__")).desc(nulls_last=True) if x['desc'] else F(x['id'].replace(".", "__")).asc(nulls_last=True)
            for x in sort_objs if x['id'] in to_use_order_fields
        ]
        if order_arguments:
            results: QuerySet[Archive] = Archive.objects.order_by(*order_arguments)
        else:
            results = Archive.objects.order_by(F("gallery__posted").desc(nulls_last=True))
    else:
        if authenticated:
            to_use_order_fields = archive_order_fields
        else:
            to_use_order_fields = archive_public_order_fields
        # sort and filter results by parameters
        order = "posted"
        if params["sort"] and params["sort"] in to_use_order_fields:
            order = params["sort"]
        if order == 'rating':
            order = 'gallery__' + order
        elif order == 'posted':
            order = 'gallery__' + order
        elif order == 'category':
            order = 'gallery__' + order

        if params["asc_desc"] == "desc":
            # order = '-' + order
            order_argument = F(order).desc(nulls_last=True)
        else:
            order_argument = F(order).asc(nulls_last=True)

        results = Archive.objects.order_by(order_argument)

    if params["title"]:
        q_formatted = '%' + params["title"].replace(' ', '%') + '%'
        results = results.filter(
            Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
        )
    if params["filename"]:
        results = results.filter(zipped__icontains=params["filename"])
    if params["rating_from"]:
        results = results.filter(gallery__rating__gte=float(params["rating_from"]))
    if params["rating_to"]:
        results = results.filter(gallery__rating__lte=float(params["rating_to"]))
    if params["filecount_from"]:
        results = results.filter(filecount__gte=int(float(params["filecount_from"])))
    if params["filecount_to"]:
        results = results.filter(filecount__lte=int(float(params["filecount_to"])))
    if params["posted_from"]:
        results = results.filter(gallery__posted__gte=params["posted_from"])
    if params["posted_to"]:
        results = results.filter(gallery__posted__lte=params["posted_to"])

    if authenticated:
        
        if "extracted" in params and params["extracted"]:
            results = results.filter(extracted=True)
        if "created_from" in params and params["created_from"]:
            results = results.filter(create_date__gte=params["created_from"])
        if "created_to" in params and params["created_to"]:
            results = results.filter(create_date__lte=params["created_to"])

    if params["match_type"]:
        results = results.filter(match_type__icontains=params["match_type"])
    if params["source_type"]:
        if "," in params["source_type"]:
            source_types = params["source_type"].split(',')
            for source_type in source_types:
                source_type = source_type.strip()
                if source_type.startswith("-"):
                    results = results.exclude(source_type__icontains=source_type[1:])
                else:
                    results = results.filter(source_type__icontains=source_type)
        else:
            if params["source_type"].startswith("-"):
                results = results.exclude(source_type__icontains=params["source_type"][1:])
            else:
                results = results.filter(source_type__icontains=params["source_type"])
    if params["reason"]:
        if "," in params["reason"]:
            reasons = params["reason"].split(',')
            for reason in reasons:
                reason = reason.strip()
                if reason.startswith("-"):
                    results = results.exclude(reason__icontains=reason[1:])
                else:
                    results = results.filter(reason__icontains=reason)
        else:
            if params["reason"].startswith("-"):
                results = results.filter(reason__icontains=params["reason"][1:])
            else:
                results = results.filter(reason__icontains=params["reason"])
    if params["uploader"]:
        results = results.filter(gallery__uploader__icontains=params["uploader"])
    if params["category"]:
        results = results.filter(gallery__category__icontains=params["category"])

    if params["tags"]:
        tags = params["tags"].split(',')
        for tag in tags:
            tag = tag.strip().replace(" ", "_")
            tag_clean = re.sub("^[-|^]", "", tag)
            scope_name = tag_clean.split(":", maxsplit=1)
            if len(scope_name) > 1:
                tag_scope = scope_name[0]
                tag_name = scope_name[1]
            else:
                tag_scope = ''
                tag_name = scope_name[0]
            if tag.startswith("-"):
                if tag_name != '' and tag_scope != '':
                    tag_query = (
                        Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)
                    )
                elif tag_name != '':
                    tag_query = Q(tags__name__contains=tag_name)
                else:
                    tag_query = Q(tags__scope__contains=tag_scope)

                results = results.exclude(
                    tag_query
                )
            elif tag.startswith("^"):
                if tag_name != '' and tag_scope != '':
                    tag_query = (
                        Q(tags__name__exact=tag_name) & Q(tags__scope__exact=tag_scope)
                    )
                elif tag_name != '':
                    tag_query = Q(tags__name__exact=tag_name)
                else:
                    tag_query = Q(tags__scope__exact=tag_scope)

                results = results.filter(
                    tag_query
                )
            else:
                if tag_name != '' and tag_scope != '':
                    tag_query = (
                        Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)
                    )
                elif tag_name != '':
                    tag_query = Q(tags__name__contains=tag_name)
                else:
                    tag_query = Q(tags__scope__contains=tag_scope)

                results = results.filter(
                    tag_query
                )

        results = results.distinct()

    if "non_public" in params and params["non_public"]:
        results = results.filter(public=False)

    if "public" in params and params["public"]:
        results = results.filter(public=True)

    if "hidden" in params and params["hidden"]:
        results = results.filter(gallery__hidden=True)

    if not show_binned:
        results = results.filter(binned=False)

    if 'view' in params:
        if params["view"] == "list":
            results = results.select_related('gallery')
        elif params["view"] == "cover":
            pass
        elif params["view"] == "extended":
            results = results.select_related('gallery').\
                prefetch_related('tags')

    return results


def filter_galleries_simple(params: dict[str, str]) -> QuerySet[Gallery]:
    """Filter results through parameters
    and return results list.
    """
    # sort and filter results by parameters
    order = "posted"
    if 'sort' in params and params["sort"]:
        order = params["sort"]
    if 'asc_desc' in params and params["asc_desc"] == "desc":
        results: QuerySet[Gallery] = Gallery.objects.order_by(F(order).desc(nulls_last=True))
    else:
        results = Gallery.objects.order_by(F(order).asc(nulls_last=True))

    if params["title"]:
        q_formatted = '%' + params["title"].replace(' ', '%') + '%'
        results = results.filter(
            Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
        )
    if params["rating_from"]:
        results = results.filter(rating__gte=float(params["rating_from"]))
    if params["rating_to"]:
        results = results.filter(rating__lte=float(params["rating_to"]))
    if params["filecount_from"]:
        results = results.filter(filecount__gte=int(float(params["filecount_from"])))
    if params["filecount_to"]:
        results = results.filter(filecount__lte=int(float(params["filecount_to"])))
    if params["filesize_from"]:
        results = results.filter(filesize__gte=float(params["filesize_from"]))
    if params["filesize_to"]:
        results = results.filter(filesize__lte=float(params["filesize_to"]))
    if params["category"]:
        results = results.filter(category__icontains=params["category"])
    if params["expunged"]:
        results = results.filter(expunged=params["expunged"])
    if params["hidden"]:
        results = results.filter(hidden=params["hidden"])
    if params["fjord"]:
        results = results.filter(fjord=params["fjord"])
    if params["uploader"]:
        results = results.filter(uploader=params["uploader"])
    if params["dl_type"]:
        results = results.filter(dl_type=params["dl_type"])
    if params["reason"]:
        results = results.filter(reason__contains=params["reason"])
    if params["provider"]:
        results = results.filter(provider=params["provider"])
    if params["not_used"]:
        results = results.filter(Q(archive__isnull=True))
    if params["reason"]:
        results = results.filter(reason__contains=params["reason"])

    if params["tags"]:
        tags = params["tags"].split(',')
        for tag in tags:
            tag = tag.strip().replace(" ", "_")
            tag_clean = re.sub("^[-|^]", "", tag)
            scope_name = tag_clean.split(":", maxsplit=1)
            if len(scope_name) > 1:
                tag_scope = scope_name[0]
                tag_name = scope_name[1]
            else:
                tag_scope = ''
                tag_name = scope_name[0]
            if tag.startswith("-"):
                if tag_name != '' and tag_scope != '':
                    tag_query = Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)
                elif tag_name != '':
                    tag_query = Q(tags__name__contains=tag_name)
                else:
                    tag_query = Q(tags__scope__contains=tag_scope)

                results = results.exclude(
                    tag_query
                )
            elif tag.startswith("^"):
                if tag_name != '' and tag_scope != '':
                    tag_query = Q(tags__name__exact=tag_name) & Q(tags__scope__exact=tag_scope)
                elif tag_name != '':
                    tag_query = Q(tags__name__exact=tag_name)
                else:
                    tag_query = Q(tags__scope__exact=tag_scope)

                results = results.filter(
                    tag_query
                )
            else:
                if tag_name != '' and tag_scope != '':
                    tag_query = Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)
                elif tag_name != '':
                    tag_query = Q(tags__name__contains=tag_name)
                else:
                    tag_query = Q(tags__scope__contains=tag_scope)

                results = results.filter(
                    tag_query
                )

        results = results.distinct()

    if "non_public" in params and params["non_public"]:
        results = results.filter(public=False)

    if "public" in params and params["public"]:
        results = results.filter(public=True)

    return results


def filter_wanted_galleries_simple(params: dict[str, Any]) -> QuerySet[WantedGallery]:
    """Filter results through parameters
    and return results list.
    """
    # sort and filter results by parameters
    order = "release_date"
    if 'sort' in params and params["sort"] and params["sort"] in wanted_gallery_order_fields:
        order = params["sort"]
    if 'asc_desc' in params and params["asc_desc"] == "desc":
        order = '-' + order

    results: QuerySet[WantedGallery] = WantedGallery.objects.order_by(order)

    if params["title"]:
        q_formatted = '%' + params["title"].replace(' ', '%') + '%'
        results = results.filter(
            Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted) | Q(search_title__ss=q_formatted) | Q(unwanted_title__ss=q_formatted)
        )
    if params["wanted_page_count_lower"]:
        results = results.filter(wanted_page_count_lower=int(params["wanted_page_count_lower"]))
    if params["wanted_page_count_upper"]:
        results = results.filter(wanted_page_count_upper=int(params["wanted_page_count_upper"]))
    if params["provider"]:
        results = results.filter(wanted_providers__slug=params["provider"]).distinct()
    if params["not_used"]:
        results = results.filter(Q(archive__isnull=True))
    if params['wanted-should-search']:
        results = results.filter(should_search=True)
    if params['wanted-should-search-not']:
        results = results.filter(should_search=False)
    if params['book_type']:
        results = results.filter(book_type=params['book_type'])
    if params['publisher']:
        results = results.filter(publisher=params['publisher'])
    if params['reason']:
        results = results.filter(reason=params['reason'])
    if params['wanted-found']:
        results = results.filter(found=True)
    if params['wanted-not-found']:
        results = results.filter(found=False)
    if params['wanted-no-found-galleries']:
        results = results.annotate(founds=Count('found_galleries')).filter(founds=0)
    if params['with-possible-matches']:
        results = results.annotate(
            num_possible=Count('possible_matches')).filter(num_possible__gt=0)
    if params["mention-source"]:
        results = results.filter(mentions__source=params["mention-source"]).distinct()

    if params["tags"]:
        tags = params["tags"].split(',')
        for tag in tags:
            tag = tag.strip().replace(" ", "_")
            tag_clean = re.sub("^[-|^]", "", tag)
            scope_name = tag_clean.split(":", maxsplit=1)
            if len(scope_name) > 1:
                tag_scope = scope_name[0]
                tag_name = scope_name[1]
            else:
                tag_scope = ''
                tag_name = scope_name[0]
            if tag.startswith("-"):
                if tag_name != '' and tag_scope != '':
                    tag_query = (
                        (Q(wanted_tags__name__contains=tag_name) & Q(wanted_tags__scope__contains=tag_scope))
                        | (Q(unwanted_tags__name__contains=tag_name) & Q(unwanted_tags__scope__contains=tag_scope))
                    )
                elif tag_name != '':
                    tag_query = (Q(wanted_tags__name__contains=tag_name) | Q(unwanted_tags__name__contains=tag_name))
                else:
                    tag_query = (Q(wanted_tags__scope__contains=tag_scope) | Q(unwanted_tags__scope__contains=tag_scope))
                results = results.exclude(
                    tag_query
                )
            elif tag.startswith("^"):
                if tag_name != '' and tag_scope != '':
                    tag_query = (
                        (Q(wanted_tags__name__exact=tag_name) & Q(wanted_tags__scope__exact=tag_scope))
                        | (Q(unwanted_tags__name__exact=tag_name) & Q(unwanted_tags__scope__exact=tag_scope))
                    )
                elif tag_name != '':
                    tag_query = (Q(wanted_tags__name__exact=tag_name) | Q(unwanted_tags__name__exact=tag_name))
                else:
                    tag_query = (Q(wanted_tags__scope__exact=tag_scope) | Q(unwanted_tags__scope__exact=tag_scope))
                results = results.filter(
                    tag_query
                )
            else:
                if tag_name != '' and tag_scope != '':
                    tag_query = (
                        (Q(wanted_tags__name__contains=tag_name) & Q(wanted_tags__scope__contains=tag_scope))
                        | (Q(unwanted_tags__name__contains=tag_name) & Q(unwanted_tags__scope__contains=tag_scope))
                    )
                elif tag_name != '':
                    tag_query = (Q(wanted_tags__name__contains=tag_name) | Q(unwanted_tags__name__contains=tag_name))
                else:
                    tag_query = (Q(wanted_tags__scope__contains=tag_scope) | Q(unwanted_tags__scope__contains=tag_scope))
                results = results.filter(
                    tag_query
                )

        results = results.distinct()

    return results


def render_error(request: HttpRequest, message: str) -> HttpResponseRedirect:
    messages.error(request, message, extra_tags='danger')
    if 'HTTP_REFERER' in request.META:
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    else:
        return HttpResponseRedirect(reverse('viewer:main-page'))


def render_message(request: HttpRequest, message: str) -> HttpResponse:

    return render(
        request,
        'viewer/message.html',
        {"message": message}
    )


def about(request: HttpRequest) -> HttpResponse:

    return render(
        request,
        'viewer/about.html'
    )
