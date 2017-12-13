import json
import logging
import re
import threading
from functools import reduce
from random import randint

import operator
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.mail import mail_admins
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.urls import reverse
from django.db.models import Q, Avg, Max, Min, Sum, Count, Prefetch
from django.http import Http404
from django.http import HttpResponseRedirect, HttpResponse
from django.http.request import QueryDict
from django.shortcuts import redirect, render
from django.conf import settings
from django.utils.html import urlize

from core.base.setup import Settings
from core.base.utilities import (
    thread_exists)

from viewer.forms import (
    ArchiveSearchForm,
    GallerySearchForm,
    SpanErrorList, ArchiveSearchSimpleForm,
    WantedGallerySearchForm)
from viewer.models import (
    Archive, Image, Tag, Gallery,
    UserArchivePrefs, ArchiveMatches,
    WantedGallery, GalleryMatch
)
from viewer.utils.matching import (
    create_matches_wanted_galleries_from_providers,
    create_matches_wanted_galleries_from_providers_internal,
    generate_possible_matches_for_archives)
from viewer.utils.tags import sort_tags

crawler_logger = logging.getLogger('viewer.webcrawler')
folder_logger = logging.getLogger('viewer.foldercrawler')
frontend_logger = logging.getLogger('viewer.frontend')
crawler_settings = settings.CRAWLER_SETTINGS

gallery_filter_keys = (
    "title", "rating_from", "rating_to", "filesize_from",
    "filesize_to", "filecount_from", "filecount_to", "posted_from", "posted_to",
    "category", "provider", "dl_type",
    "expunged", "hidden", "fjord", "uploader", "tags", "not_used"
)

archive_filter_keys = (
    "title", "filename", "filecount_from", "filecount_to",
    "rating_from", "rating_to", "match_type", "posted_from",
    "posted_to", "source_type", "tags", "only_favorites",
    "non_public", "public", "reason",
    "uploader",
    "qsearch"
)

wanted_gallery_filter_keys = (
    "title", "wanted_page_count_lower", "wanted_page_count_upper",
    "provider", "not_used", "wanted-should-search", "wanted-should-search-not", "book_type",
    "publisher", "wanted-found", "wanted-not-found", "reason",
    "wanted-no-found-galleries", "with-possible-matches", "tags"
)


def viewer_login(request):

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
        return render(request, 'viewer/login.html', d)


@login_required
def viewer_logout(request):
    logout(request)
    return HttpResponseRedirect(reverse('viewer:main-page'))


def session_settings(request):
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
@login_required
def panda_userscript(request):

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


@login_required
def image_viewer(request, archive, page):

    images = Image.objects.filter(archive=archive, extracted=True)
    if not images:
        raise Http404("Archive " + str(archive) + " has no extracted images")

    paginator = Paginator(images, 1)
    try:
        image = paginator.page(page)
    except (InvalidPage, EmptyPage):
        image = paginator.page(paginator.num_pages)

    image_object = image.object_list[0]

    if image_object.image_width / image_object.image_height > 1:
        image_object.is_horizontal = True

    d = {'image': image, 'backurl': redirect(image.object_list[0].archive).url,
         'images_range': range(1, images.count() + 1), 'image_object': image_object}

    return render(request, "viewer/image_viewer.html", d)


def image_url(request, pk):
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


def gallery_details(request, pk, tool=None):
    try:
        gallery = Gallery.objects.get(pk=pk)
    except Gallery.DoesNotExist:
        raise Http404("Gallery does not exist")
    if not (gallery.public or request.user.is_authenticated):
        raise Http404("Gallery does not exist")

    if request.user.is_staff and tool == "download":
        if 'downloader' in request.GET:
            current_settings = Settings(load_from_config=crawler_settings.config)
            current_settings.allow_downloaders_only([request.GET['downloader']], True, True, True)
            current_settings.workers.web_queue.enqueue_args_list((gallery.get_link(),), override_options=current_settings)
        else:
            # Since this is used from the gallery page mainly to download an already added gallery using
            # downloader settings, force replace_metadata
            current_settings = Settings(load_from_config=crawler_settings.config)
            current_settings.replace_metadata = True
            current_settings.workers.web_queue.enqueue_args_list((gallery.get_link(),), override_options=current_settings)
        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    if request.user.is_staff and tool == "toggle-hidden":
        gallery.hidden = not gallery.hidden
        gallery.save()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    if request.user.is_staff and tool == "toggle-public":
        gallery.public_toggle()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    if request.user.is_staff and tool == "mark-deleted":
        gallery.mark_as_deleted()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    if request.user.is_staff and tool == "recall-api":

        current_settings = Settings(load_from_config=crawler_settings.config)

        current_settings.set_update_metadata_options(providers=(gallery.provider,))

        current_settings.workers.web_queue.enqueue_args_list((gallery.get_link(),), override_options=current_settings)

        frontend_logger.info(
            'Updating gallery API data for gallery: {} and related archives'.format(
                gallery.get_absolute_url()
            )
        )

        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    tag_lists = sort_tags(gallery.tags.all())

    d = {'gallery': gallery, 'tag_lists': tag_lists, 'settings': crawler_settings}
    return render(request, "viewer/gallery.html", d)


def gallery_thumb(request, pk):
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


def gallery_list(request, mode='none', tag=None):
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
    request.GET = cleared_queries

    display_prms = {}

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

    if "clear" in get:
        for k in gallery_filter_keys:
            display_prms[k] = ''
        request.GET = QueryDict('')
        # request.session["gallery_parameters"].clear()
    else:
        if mode == 'gallery-tag' and tag:
            display_prms['tags'] = tag
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
    results = filter_galleries(request, parameters, display_prms)

    if "random" in get:
        count = results.count()
        if count > 0:
            random_index = randint(0, count - 1)
            return redirect(results[random_index])

    # make paginator
    galleries_per_page = 100

    paginator = Paginator(results, galleries_per_page)
    try:
        results = paginator.page(page)

    except (InvalidPage, EmptyPage):
        results = paginator.page(1)

    display_prms['page_range'] = list(
        range(
            max(1, results.number - 3 - max(0, results.number - (paginator.num_pages - 3))),
            min(paginator.num_pages + 1, results.number + 3 + 1 - min(0, results.number - 3 - 1))
        )
    )

    form = GallerySearchForm(initial={'title': display_prms['title'],
                                      'tags': display_prms['tags']})

    d = {
        'results': results, 'prm': parameters,
        'display_prms': display_prms, 'form': form
    }

    return render(request, "viewer/gallery_search.html", d)


def filter_galleries(request, session_filters, request_filters):
    """Filter gallery results through parameters and return results list."""

    # sort and filter results by parameters
    order = "posted"
    if session_filters["sort"]:
        order = session_filters["sort"]
    if session_filters["asc_desc"] == "desc":
        order = '-' + order

    results = Gallery.objects.eligible_for_use().order_by(order)

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
        results = results.filter(filecount__gte=int(request_filters["filecount_from"]))
    if request_filters["filecount_to"]:
        results = results.filter(filecount__lte=int(request_filters["filecount_to"]))
    if request_filters["filesize_from"]:
        results = results.filter(filesize__gte=float(request_filters["filesize_from"]))
    if request_filters["filesize_to"]:
        results = results.filter(filesize__lte=float(request_filters["filesize_to"]))
    if request_filters["category"]:
        results = results.filter(category__icontains=request_filters["category"])
    if request_filters["expunged"]:
        results = results.filter(expunged=request_filters["expunged"])
    if request_filters["hidden"]:
        results = results.filter(hidden=request_filters["hidden"])
    if request_filters["fjord"]:
        results = results.filter(fjord=request_filters["fjord"])
    if request_filters["uploader"]:
        results = results.filter(uploader=request_filters["uploader"])
    if request_filters["provider"]:
        results = results.filter(provider=request_filters["provider"])
    if request_filters["not_used"]:
        results = results.filter(Q(archive__isnull=True))

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
                results = results.exclude(
                    Q(tags__name__contains=tag_name),
                    Q(tags__scope__contains=tag_scope))
            elif tag.startswith("^"):
                results = results.filter(
                    Q(tags__name__exact=tag_name),
                    Q(tags__scope__exact=tag_scope))
            else:
                results = results.filter(
                    Q(tags__name__contains=tag_name),
                    Q(tags__scope__contains=tag_scope))

    results = results.distinct()

    return results


def search(request, mode='none', tag=None):
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

    if 'rss' in get:
        return redirect('viewer:archive-rss')

    request.GET = cleared_queries

    display_prms = {}

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
            display_prms['tags'] = tag
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

    # make paginator
    if parameters['view'] == 'list':
        archives_per_page = 100
    else:
        archives_per_page = 24

    paginator = Paginator(results, archives_per_page)
    try:
        results = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(1)

    display_prms['page_range'] = list(
        range(
            max(1, results.number - 3 - max(0, results.number - (paginator.num_pages - 3))),
            min(paginator.num_pages + 1, results.number + 3 + 1 - min(0, results.number - 3 - 1))
        )
    )

    d = {
        'results': results, 'extra_options': view_options,
        'prm': parameters, 'display_prms': display_prms,
        'form': form, 'form_simple': form_simple
    }
    return render(request, "viewer/archive_search.html", d)


def filter_archives(request, session_filters, request_filters):
    """Filter results through parameters
    and return results list.
    """
    # sort and filter results by parameters
    order = "posted"
    if session_filters["sort"]:
        order = session_filters["sort"]
    if order == 'rating':
        order = 'gallery__' + order
    elif order == 'posted':
        order = 'gallery__' + order
    if session_filters["asc_desc"] == "desc":
        order = '-' + order

    results = Archive.objects.order_by(order)

    if not request.user.is_authenticated:
        results = results.filter(public=True)

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
        results = results.filter(filecount__gte=int(request_filters["filecount_from"]))
    if request_filters["filecount_to"]:
        results = results.filter(filecount__lte=int(request_filters["filecount_to"]))
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
                tag_query = (
                    (Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)) |
                    (Q(custom_tags__name__contains=tag_name) & Q(custom_tags__scope__contains=tag_scope))
                ) if tag_scope != '' else (Q(tags__name__contains=tag_name) | Q(custom_tags__name__contains=tag_name))
                results = results.exclude(
                    tag_query
                )
            elif tag.startswith("^"):
                tag_query = (
                    (Q(tags__name__exact=tag_name) & Q(tags__scope__exact=tag_scope)) |
                    (Q(custom_tags__name__exact=tag_name) & Q(custom_tags__scope__exact=tag_scope))
                ) if tag_scope != '' else (Q(tags__name__exact=tag_name) | Q(custom_tags__name__exact=tag_name))
                results = results.filter(
                    tag_query
                )
            else:
                tag_query = (
                    (Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)) |
                    (Q(custom_tags__name__contains=tag_name) & Q(custom_tags__scope__contains=tag_scope))
                ) if tag_scope != '' else (Q(tags__name__contains=tag_name))
                results = results.filter(
                    tag_query
                )
    if "only_favorites" in request_filters and request_filters["only_favorites"]:
        user_arch_ids = UserArchivePrefs.objects.filter(
            user=request.user.id, favorite_group__gt=0).values_list('archive')
        results = results.filter(id__in=user_arch_ids)

    if "non_public" in request_filters and request_filters["non_public"]:
        results = results.filter(public=False)

    if "public" in request_filters and request_filters["public"]:
        results = results.filter(public=True)

    if session_filters["view"] == "list":
        results = results.distinct().select_related('gallery')
    elif session_filters["view"] == "cover":
        results = results.distinct()
    elif session_filters["view"] == "extended":
        results = results.distinct().select_related('gallery').\
            prefetch_related(
                'tags').prefetch_related('custom_tags')
    else:
        results = results.distinct()
    return results


def quick_search(request, parameters, display_parameters):
    """Quick search of archives."""
    # sort and filter results by parameters
    order = "posted"
    if parameters["sort"]:
        order = parameters["sort"]
    if order == 'rating':
        order = 'gallery__' + order
    if order == 'posted':
        order = 'gallery__' + order
    if parameters["asc_desc"] == "desc":
        order = '-' + order

    results = Archive.objects.order_by(order)

    if not request.user.is_authenticated:
        results = results.filter(public=True)

    # URL search
    url = display_parameters["qsearch"]

    parsers = crawler_settings.provider_context.get_parsers_classes()
    gallery_ids_providers = list()
    for parser in parsers:
        if hasattr(parser, 'id_from_url'):
            accepted_urls = parser.filter_accepted_urls((url,))
            gallery_ids_providers.extend([(parser.id_from_url(x), parser.name) for x in accepted_urls])

    if gallery_ids_providers:
        query = reduce(
            operator.or_,
            (Q(gallery__gid=gid, gallery__provider=provider) for gid, provider in gallery_ids_providers)
        )

        results_url = results.filter(query)
    else:
        results_url = None

    q_formatted = '%' + display_parameters["qsearch"].replace(' ', '%') + '%'
    results_title = results.filter(
        Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
    )

    tags = display_parameters["qsearch"].split(',')
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
            results = results.exclude(
                (Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)) |
                (Q(custom_tags__name__contains=tag_name) & Q(custom_tags__scope__contains=tag_scope))
            )
        elif tag.startswith("^"):
            results = results.filter(
                (Q(tags__name__exact=tag_name) & Q(tags__scope__exact=tag_scope)) |
                (Q(custom_tags__name__exact=tag_name) & Q(custom_tags__scope__exact=tag_scope))
            )
        else:
            results = results.filter(
                (Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)) |
                (Q(custom_tags__name__contains=tag_name) & Q(custom_tags__scope__contains=tag_scope))
            )
    if results_url:
        results = results | results_title | results_url
    else:
        results = results | results_title

    if "non_public" in display_parameters and display_parameters["non_public"]:
        results = results.filter(public=False)

    if "public" in display_parameters and display_parameters["public"]:
        results = results.filter(public=True)

    if parameters["view"] == "list":
        results = results.distinct().select_related('gallery')
    elif parameters["view"] == "cover":
        results = results.distinct()
    elif parameters["view"] == "extended":
        results = results.distinct().select_related('gallery').\
            prefetch_related(
                'tags').prefetch_related('custom_tags')
    else:
        results = results.distinct()
    return results


# TODO: Submissions done before a autocrawler detects a new gallery as wanted,
# will add the gallery, trigger the wanted filter, but leaving it as not downloaded.
def url_submit(request):
    """Submit given URLs."""

    if not crawler_settings.urls.enable_public_submit:
        if not request.user.is_staff:
            raise Http404("Page not found")
        else:
            return render_error(request, "Page disabled by settings (urls: enable_public_submit).")

    p = request.POST

    if p:
        current_settings = Settings(load_from_config=crawler_settings.config)
        url_set = set()
        # create dictionary of properties for each url
        current_settings.replace_metadata = False
        current_settings.config['allowed']['replace_metadata'] = 'no'
        for k, v in current_settings.config['downloaders'].items():
                current_settings.config['downloaders'][k] = str(-1)
                current_settings.downloaders[k] = -1
        current_settings.config['downloaders']['panda_submit'] = str(1)
        current_settings.downloaders['panda_submit'] = 1
        for k, v in p.items():
            if k == "urls":
                url_list = v.split("\n")
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
            current_settings.force_dl_type = reason[:93]

        # As a security check, only finally set urls that pass the accepted_urls
        parsers = crawler_settings.provider_context.get_parsers_classes()
        no_commands_in_args_list = list()
        for parser in parsers:
            if hasattr(parser, 'id_from_url'):
                no_commands_in_args_list.extend(parser.filter_accepted_urls(urls))
        current_settings.workers.web_queue.enqueue_args_list(no_commands_in_args_list, override_options=current_settings)

        url_messages = []
        admin_messages = []

        # Sometimes people submit messages here.
        found_valid_urls = []

        for parser in parsers:
            if hasattr(parser, 'id_from_url'):
                urls_filtered = parser.filter_accepted_urls(urls)
                found_valid_urls.extend(urls_filtered)
                for url_filtered in urls_filtered:
                    gid = parser.id_from_url(url_filtered)
                    gallery = Gallery.objects.filter(gid=gid).first()
                    if not gallery:
                        url_messages.append('{}: New URL, will be added to the submit queue'.format(
                            url_filtered
                        ))
                        messages.success(
                            request,
                            'URL {} was not in the backup and will be added to the submit queue.'.format(url_filtered)
                        )
                        continue
                    if 'submit' in gallery.dl_type:
                        messages.info(
                            request,
                            'URL {} already exists in the backup and it\'s being reviewed.'.format(url_filtered)
                        )
                        url_messages.append('{}: Already in submit queue, reason: {}'.format(
                            url_filtered, gallery.dl_type.replace("submit:", ""))
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

        if reason:
            admin_subject = 'New submission, reason: {}'.format(reason)
        else:
            admin_subject = 'New submission, no reason given'

        if url_messages:
            admin_messages.append('URL details:\n{}'.format("\n".join(url_messages)))

        if extra_text:
            admin_messages.append('Extra non-URL text:\n{}'.format("\n".join(extra_text)))

        if current_settings.mail_logging.enable:
            mail_admins(admin_subject, "\n".join(admin_messages), html_message=urlize("\n".join(admin_messages)))

        frontend_logger.info("{}\n{}".format(admin_subject, "\n".join(admin_messages)))

        return HttpResponseRedirect(reverse('viewer:url-submit'))

    return render(request, "viewer/url_submit.html")


def public_stats(request):
    """Display public galleries and archives stats."""
    if not crawler_settings.urls.enable_public_stats:
        if not request.user.is_staff:
            raise Http404("Page not found")
        else:
            return render_error(request, "Page disabled by settings (urls: enable_public_stats).")

    stats_dict = {
        "n_archives": Archive.objects.filter(public=True).count(),
        "archive": Archive.objects.filter(public=True).filter(filesize__gt=0).aggregate(
            Avg('filesize'), Max('filesize'), Min('filesize'), Sum('filesize')),
        "n_tags": Tag.objects.filter(gallery_tags__public=True).distinct().count(),
        "top_10_tags": Tag.objects.filter(gallery_tags__public=True).distinct().annotate(
            num_archive=Count('gallery_tags')).order_by('-num_archive')[:10]
    }

    d = {'stats': stats_dict}

    return render(request, "viewer/public_stats.html", d)


@login_required
def user_archive_preferences(request, archive_pk, setting):
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


def filter_archives_simple(params):
    """Filter results through parameters
    and return results list.
    """
    # sort and filter results by parameters
    order = "posted"
    if params["sort"]:
        order = params["sort"]
    if order == 'rating':
        order = 'gallery__' + order
    elif order == 'posted':
        order = 'gallery__' + order
    if params["asc_desc"] == "desc":
        order = '-' + order

    results = Archive.objects.order_by(order)

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
        results = results.filter(filecount__gte=int(params["filecount_from"]))
    if params["filecount_to"]:
        results = results.filter(filecount__lte=int(params["filecount_to"]))
    if params["posted_from"]:
        results = results.filter(gallery__posted__gte=params["posted_from"])
    if params["posted_to"]:
        results = results.filter(gallery__posted__lte=params["posted_to"])
    if params["match_type"]:
        results = results.filter(match_type__icontains=params["match_type"])
    if params["source_type"]:
        results = results.filter(source_type__icontains=params["source_type"])
    if params["reason"]:
        results = results.filter(reason__icontains=params["reason"])
    if params["uploader"]:
        results = results.filter(gallery__uploader__icontains=params["uploader"])

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
                tag_query = (
                    (Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)) |
                    (Q(custom_tags__name__contains=tag_name) & Q(custom_tags__scope__contains=tag_scope))
                ) if tag_scope != '' else (Q(tags__name__contains=tag_name) | Q(custom_tags__name__contains=tag_name))
                results = results.exclude(
                    tag_query
                )
            elif tag.startswith("^"):
                tag_query = (
                    (Q(tags__name__exact=tag_name) & Q(tags__scope__exact=tag_scope)) |
                    (Q(custom_tags__name__exact=tag_name) & Q(custom_tags__scope__exact=tag_scope))
                ) if tag_scope != '' else (Q(tags__name__exact=tag_name) | Q(custom_tags__name__exact=tag_name))
                results = results.filter(
                    tag_query
                )
            else:
                tag_query = (
                    (Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)) |
                    (Q(custom_tags__name__contains=tag_name) & Q(custom_tags__scope__contains=tag_scope))
                ) if tag_scope != '' else (Q(tags__name__contains=tag_name))
                results = results.filter(
                    tag_query
                )

    if "non_public" in params and params["non_public"]:
        results = results.filter(public=False)

    if "public" in params and params["public"]:
        results = results.filter(public=True)

    if 'view'in params:
        if params["view"] == "list":
            results = results.distinct().select_related('gallery')
        elif params["view"] == "cover":
            results = results.distinct()
        elif params["view"] == "extended":
            results = results.distinct().select_related('gallery').\
                prefetch_related('tags').prefetch_related('custom_tags')
        else:
            results = results.distinct()
    else:
        results = results.distinct()

    return results


def filter_galleries_simple(params):
    """Filter results through parameters
    and return results list.
    """
    # sort and filter results by parameters
    order = "posted"
    if 'sort' in params and params["sort"]:
        order = params["sort"]
    if 'asc_desc' in params and params["asc_desc"] == "desc":
        order = '-' + order

    results = Gallery.objects.order_by(order)

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
        results = results.filter(filecount__gte=int(params["filecount_from"]))
    if params["filecount_to"]:
        results = results.filter(filecount__lte=int(params["filecount_to"]))
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
    if params["provider"]:
        results = results.filter(provider=params["provider"])
    if params["not_used"]:
        results = results.filter(Q(archive__isnull=True))

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
                results = results.exclude(
                    Q(tags__name__contains=tag_name),
                    Q(tags__scope__contains=tag_scope))
            elif tag.startswith("^"):
                results = results.filter(
                    Q(tags__name__exact=tag_name),
                    Q(tags__scope__exact=tag_scope))
            else:
                results = results.filter(
                    Q(tags__name__contains=tag_name),
                    Q(tags__scope__contains=tag_scope))

    if "non_public" in params and params["non_public"]:
        results = results.filter(public=False)

    if "public" in params and params["public"]:
        results = results.filter(public=True)

    results = results.distinct()

    return results


def filter_wanted_galleries_simple(params):
    """Filter results through parameters
    and return results list.
    """
    # sort and filter results by parameters
    order = "release_date"
    if 'sort' in params and params["sort"]:
        order = params["sort"]
    if 'asc_desc' in params and params["asc_desc"] == "desc":
        order = '-' + order

    results = WantedGallery.objects.order_by(order)

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
        results = results.filter(provider=params["provider"])
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
                tag_query = (
                    (Q(wanted_tags__name__contains=tag_name) & Q(wanted_tags__scope__contains=tag_scope)) |
                    (Q(unwanted_tags__name__contains=tag_name) & Q(unwanted_tags__scope__contains=tag_scope))
                ) if tag_scope != '' else (Q(wanted_tags__name__contains=tag_name) | Q(unwanted_tags__name__contains=tag_name))
                results = results.exclude(
                    tag_query
                )
            elif tag.startswith("^"):
                tag_query = (
                    (Q(wanted_tags__name__exact=tag_name) & Q(wanted_tags__scope__exact=tag_scope)) |
                    (Q(unwanted_tags__name__exact=tag_name) & Q(unwanted_tags__scope__exact=tag_scope))
                ) if tag_scope != '' else (Q(wanted_tags__name__exact=tag_name) | Q(unwanted_tags__name__exact=tag_name))
                results = results.filter(
                    tag_query
                )
            else:
                tag_query = (
                    (Q(wanted_tags__name__contains=tag_name) & Q(wanted_tags__scope__contains=tag_scope)) |
                    (Q(unwanted_tags__name__contains=tag_name) & Q(unwanted_tags__scope__contains=tag_scope))
                ) if tag_scope != '' else (Q(wanted_tags__name__contains=tag_name) | Q(unwanted_tags__name__contains=tag_name))
                results = results.filter(
                    tag_query
                )

    results = results.distinct()

    return results


def lists(request, list_name):
    """Lists."""
    if not request.user.is_staff and list_name not in ("missing-archives", "wanted-galleries"):
        return render_error(request, "You need to be an admin to display the lists.")
    if not list_name:
        return render_error(request, "Need to specify the list.")
    elif list_name == "repeated-archives":
        p = request.POST
        get = request.GET

        title = get.get("title", '')
        tags = get.get("tags", '')

        try:
            page = int(get.get("page", '1'))
        except ValueError:
            page = 1

        if 'clear' in get:
            form = GallerySearchForm()
        else:
            form = GallerySearchForm(initial={'title': title, 'tags': tags})

        if p:
            pks = []
            for k, v in p.items():
                if k.startswith("del-"):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    pks.extend(p.getlist(k))
            results = Archive.objects.filter(id__in=pks).order_by('-pk')

            for archive in results:
                if 'delete_archives_and_files' in p:
                    message = 'Removing archive and deleting file: {}'.format(archive.zipped.name)
                    frontend_logger.info(message)
                    messages.success(request, message)
                    archive.delete_all_files()
                else:
                    message = 'Removing archive: {}'.format(archive.zipped.name)
                    frontend_logger.info(message)
                    messages.success(request, message)
                archive.delete()

        params = {
            'sort': 'create_date',
            'asc_desc': 'desc',
        }

        for k, v in get.items():
            params[k] = v

        for k in gallery_filter_keys:
            if k not in params:
                params[k] = ''

        results = filter_galleries_simple(params)

        results = results.several_archives()

        paginator = Paginator(results, 50)
        try:
            results = paginator.page(page)
        except (InvalidPage, EmptyPage):
            results = paginator.page(paginator.num_pages)

        d = {'results': results, 'form': form}
        return render(request, "viewer/archives_repeated.html", d)
    elif list_name == "archive-filesize-different":
        providers = Gallery.objects.all().values_list('provider', flat=True).distinct()
        p = request.POST
        get = request.GET

        title = get.get("title", '')
        tags = get.get("tags", '')

        try:
            page = int(get.get("page", '1'))
        except ValueError:
            page = 1

        if 'clear' in get:
            form = GallerySearchForm()
        else:
            form = GallerySearchForm(initial={'title': title, 'tags': tags})
        if p:
            pks = []
            for k, v in p.items():
                if k.startswith("del-"):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    pks.extend(p.getlist(k))
            results = Archive.objects.filter(id__in=pks).order_by('-pk')

            for archive in results:
                if 'delete_archives' in p:
                    message = "Removing archive: {} and keeping its file: {}".format(
                        archive.title,
                        archive.zipped.name
                    )
                    frontend_logger.info(message)
                    messages.success(request, message)
                    archive.delete()
                if 'delete_archives_and_files' in p:
                    message = "Removing archive: {} and deleting its file: {}".format(
                        archive.title,
                        archive.zipped.name
                    )
                    frontend_logger.info(message)
                    messages.success(request, message)
                    archive.delete_all_files()
                    archive.delete()

        params = {
        }

        for k, v in get.items():
            params[k] = v

        for k in gallery_filter_keys:
            if k not in params:
                params[k] = ''

        results = filter_galleries_simple(params)

        results = results.different_filesize_archive()

        paginator = Paginator(results, 50)
        try:
            results = paginator.page(page)
        except (InvalidPage, EmptyPage):
            results = paginator.page(paginator.num_pages)

        d = {'results': results, 'providers': providers, 'form': form}
        return render(request, "viewer/archives_different_filesize.html", d)
    elif list_name == "missing-archives":
        p = request.POST
        get = request.GET

        title = get.get("title", '')
        tags = get.get("tags", '')

        try:
            page = int(get.get("page", '1'))
        except ValueError:
            page = 1

        if 'clear' in get:
            form = GallerySearchForm()
        else:
            form = GallerySearchForm(initial={'title': title, 'tags': tags})

        if p and request.user.is_staff:
            pks = []
            for k, v in p.items():
                if k.startswith("sel-"):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    pks.append(v)
            results = Gallery.objects.filter(id__in=pks).order_by('-create_date')

            if 'delete_galleries' in p:
                for gallery in results:
                    message = 'Removing gallery: {}, link: {}'.format(gallery.title, gallery.get_link())
                    frontend_logger.info(message)
                    messages.success(request, message)
                    gallery.mark_as_deleted()
            elif 'download_galleries' in p:
                for gallery in results:
                    message = 'Queueing gallery: {}, link: {}'.format(gallery.title, gallery.get_link())
                    frontend_logger.info(message)
                    messages.success(request, message)

                    # Force replace_metadata when queueing from this list, since it's mostly used to download non used.
                    current_settings = Settings(load_from_config=crawler_settings.config)
                    current_settings.replace_metadata = True
                    current_settings.retry_failed = True

                    if 'reason' in p and p['reason'] != '':
                        reason = p['reason']
                        # Force limit string length (reason field max_length)
                        current_settings.archive_reason = reason[:200]

                    current_settings.workers.web_queue.enqueue_args_list((gallery.get_link(),), override_options=current_settings)

        if 'force_public' in request.GET:
            force_public = True
        else:
            force_public = False
        if request.user.is_staff and not force_public:

            providers = Gallery.objects.all().values_list('provider', flat=True).distinct()

            params = {
            }

            for k, v in get.items():
                params[k] = v

            for k in gallery_filter_keys:
                if k not in params:
                    params[k] = ''

            results = filter_galleries_simple(params)

            results = results.non_used_galleries()

            paginator = Paginator(results, 50)
            try:
                results = paginator.page(page)
            except (InvalidPage, EmptyPage):
                results = paginator.page(paginator.num_pages)

            d = {'results': results, 'providers': providers, 'force_public': force_public, 'form': form}
        else:
            results = Gallery.objects.non_used_galleries(public=True, provider__contains='panda')
            d = {'results': results}
        return render(request, "viewer/archives_missing_for_galleries.html", d)
    elif list_name == "archive-not-present":
        p = request.POST
        get = request.GET

        title = get.get("title", '')
        tags = get.get("tags", '')

        try:
            page = int(get.get("page", '1'))
        except ValueError:
            page = 1

        if 'clear' in get:
            form = ArchiveSearchForm()
        else:
            form = ArchiveSearchForm(initial={'title': title, 'tags': tags})

        if p:
            pks = []
            for k, v in p.items():
                if k.startswith("del-"):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    pks.append(v)
            results = Archive.objects.filter(id__in=pks).order_by('-pk')

            for archive in results:
                message = 'Removing archive missing in filesystem: {}, path: {}'.format(
                    archive.title, archive.zipped.path
                )
                frontend_logger.info(message)
                messages.success(request, message)
                archive.delete()

        params = {
            'sort': 'create_date',
            'asc_desc': 'desc',
            'filename': title,
        }

        for k, v in get.items():
            params[k] = v

        for k in archive_filter_keys:
            if k not in params:
                params[k] = ''

        results = filter_archives_simple(params)

        results = results.filter_non_existent(
            crawler_settings.MEDIA_ROOT
        )

        paginator = Paginator(results, 50)
        try:
            results = paginator.page(page)
        except (InvalidPage, EmptyPage):
            results = paginator.page(paginator.num_pages)

        d = {'results': results, 'form': form}
        return render(request, "viewer/archives_not_present.html", d)
    elif list_name == "archives-not-matched":
        p = request.POST
        get = request.GET

        title = get.get("title", '')
        tags = get.get("tags", '')

        try:
            page = int(get.get("page", '1'))
        except ValueError:
            page = 1

        if 'clear' in get:
            form = ArchiveSearchForm()
        else:
            form = ArchiveSearchForm(initial={'title': title, 'tags': tags})

        if p:
            pks = []
            for k, v in p.items():
                if k.startswith("sel-"):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    pks.append(v)
            archives = Archive.objects.filter(id__in=pks).order_by('-create_date')
            if 'delete_archives' in p:
                for archive in archives:
                    message = 'Removing archive not matched: {} and deleting file: {}'.format(
                        archive.title, archive.zipped.path
                    )
                    frontend_logger.info(message)
                    messages.success(request, message)
                    archive.delete_files_but_archive()
                    archive.delete()
            elif 'delete_objects' in p:
                for archive in archives:
                    message = 'Removing archive not matched: {}, keeping file: {}'.format(
                        archive.title, archive.zipped.path
                    )
                    frontend_logger.info(message)
                    messages.success(request, message)
                    archive.delete()
            elif 'create_possible_matches' in p:
                if thread_exists('web_match_worker'):
                    return render_error(request, 'Web match worker is already running.')

                matcher_filter = p['create_possible_matches']
                try:
                    cutoff = float(p.get('cutoff', '0.4'))
                except ValueError:
                    cutoff = 0.4
                try:
                    max_matches = int(p.get('max-matches', '20'))
                except ValueError:
                    max_matches = 20

                web_match_thread = threading.Thread(
                    name='web_match_worker',
                    target=generate_possible_matches_for_archives,
                    args=(archives,),
                    kwargs={
                        'logger': frontend_logger, 'cutoff': cutoff, 'max_matches': max_matches, 'filters': (matcher_filter,),
                        'match_local': False, 'match_web': True
                    })
                web_match_thread.daemon = True
                web_match_thread.start()
                messages.success(request, 'Starting web match worker.')
            elif 'create_possible_matches_internal' in p:
                if thread_exists('match_unmatched_worker'):
                    return render_error(request, "Local matching worker is already running.")
                provider = p['create_possible_matches_internal']
                try:
                    cutoff = float(p.get('cutoff', '0.4'))
                except ValueError:
                    cutoff = 0.4
                try:
                    max_matches = int(p.get('max-matches', '20'))
                except ValueError:
                    max_matches = 20

                frontend_logger.info(
                    'Looking for possible matches in gallery database '
                    'for non-matched archives (cutoff: {}, max matches: {}) '
                    'using provider filter "{}"'.format(cutoff, max_matches, provider)
                )
                matching_thread = threading.Thread(
                    name='match_unmatched_worker',
                    target=generate_possible_matches_for_archives,
                    args=(archives,),
                    kwargs={
                        'logger': frontend_logger, 'cutoff': cutoff, 'max_matches': max_matches, 'filters': (provider,),
                        'match_local': True, 'match_web': False
                    })
                matching_thread.daemon = True
                matching_thread.start()
                messages.success(request, 'Starting internal match worker.')

        params = {
            'sort': 'create_date',
            'asc_desc': 'desc',
            'filename': title,
        }

        for k, v in get.items():
            params[k] = v

        for k in archive_filter_keys:
            if k not in params:
                params[k] = ''

        results = filter_archives_simple(params)

        results = results.filter(
            gallery__isnull=True
        ).prefetch_related(
            Prefetch(
                'archivematches_set',
                queryset=ArchiveMatches.objects.select_related('gallery', 'archive').prefetch_related(
                    Prefetch(
                        'gallery__tags',
                        queryset=Tag.objects.filter(scope__exact='artist'),
                        to_attr='artist_tags'
                    )
                ),
                to_attr='possible_galleries'
            ),
            'possible_galleries__gallery',
        )

        if 'no-custom-tags' in get:
            results = results.annotate(num_custom_tags=Count('custom_tags')).filter(num_custom_tags=0)
        if 'with-possible-matches' in get:
            results = results.annotate(n_possible_matches=Count('possible_matches')).filter(n_possible_matches__gt=0)

        paginator = Paginator(results, 25)
        try:
            results = paginator.page(page)
        except (InvalidPage, EmptyPage):
            results = paginator.page(paginator.num_pages)

        d = {
            'results': results,
            'providers': Gallery.objects.all().values_list('provider', flat=True).distinct(),
            'matchers': crawler_settings.provider_context.get_matchers(crawler_settings, force=True),
            'api_key': crawler_settings.api_key,
            'form': form
        }
        return render(request, "viewer/archives_not_matched.html", d)

    elif list_name == "wanted-galleries":
        p = request.POST
        get = request.GET

        title = get.get("title", '')
        tags = get.get("tags", '')

        try:
            page = int(get.get("page", '1'))
        except ValueError:
            page = 1

        if 'clear' in get:
            form = WantedGallerySearchForm()
        else:
            form = WantedGallerySearchForm(initial={'title': title, 'tags': tags})

        if not request.user.is_staff:
            results = WantedGallery.objects.filter(
                Q(should_search=True) &
                Q(found=False) &
                Q(public=True)
            ).prefetch_related(
                'artists',
                'announces'
            ).order_by('-release_date')
            return render(request, "viewer/wanted_galleries.html", {'results': results})

        if p and request.user.is_staff:
            if 'delete_galleries' in p:
                pks = []
                for k, v in p.items():
                    if k.startswith("sel-"):
                        # k, pk = k.split('-')
                        # results[pk][k] = v
                        pks.append(v)
                results = WantedGallery.objects.filter(id__in=pks).reverse()

                for wanted_gallery in results:
                    message = 'Removing wanted gallery: {}'.format(
                        wanted_gallery.title
                    )
                    frontend_logger.info(message)
                    messages.success(request, message)
                    wanted_gallery.delete()
            elif 'search_for_galleries' in p:
                pks = []
                for k, v in p.items():
                    if k.startswith("sel-"):
                        # k, pk = k.split('-')
                        # results[pk][k] = v
                        pks.append(v)
                results = WantedGallery.objects.filter(id__in=pks).reverse()
                results.update(should_search=True)

                for wanted_gallery in results:
                    message = 'Marking gallery as to search for: {}'.format(
                        wanted_gallery.title
                    )
                    frontend_logger.info(message)
                    messages.success(request, message)
            elif 'toggle-public' in p:
                pks = []
                for k, v in p.items():
                    if k.startswith("sel-"):
                        # k, pk = k.split('-')
                        # results[pk][k] = v
                        pks.append(v)
                results = WantedGallery.objects.filter(id__in=pks).reverse()
                results.update(public=True)

                for wanted_gallery in results:
                    message = 'Marking gallery as public: {}'.format(
                        wanted_gallery.title
                    )
                    frontend_logger.info(message)
                    messages.success(request, message)
            elif 'search_provider_galleries' in p:
                if thread_exists('web_search_worker'):
                    messages.error(
                        request,
                        'Web search worker is already running.',
                        extra_tags='danger'
                    )
                    return HttpResponseRedirect(request.META["HTTP_REFERER"])
                pks = []
                for k, v in p.items():
                    if k.startswith("sel-"):
                        # k, pk = k.split('-')
                        # results[pk][k] = v
                        pks.append(v)
                results = WantedGallery.objects.filter(id__in=pks).reverse()

                provider = p.get('provider', '')

                message = 'Searching for gallery matches in providers for wanted galleries.'
                frontend_logger.info(message)
                messages.success(request, message)

                panda_search_thread = threading.Thread(
                    name='web_search_worker',
                    target=create_matches_wanted_galleries_from_providers,
                    args=(results, provider),
                    kwargs={'logger': frontend_logger})
                panda_search_thread.daemon = True
                panda_search_thread.start()
            elif 'search_provider_galleries_internal' in p:
                if thread_exists('wanted_local_search_worker'):
                    messages.error(
                        request,
                        'Wanted local matching worker is already running.',
                        extra_tags='danger'
                    )
                    return HttpResponseRedirect(request.META["HTTP_REFERER"])
                pks = []
                for k, v in p.items():
                    if k.startswith("sel-"):
                        # k, pk = k.split('-')
                        # results[pk][k] = v
                        pks.append(v)
                results = WantedGallery.objects.filter(id__in=pks).reverse()

                provider = p.get('provider', '')

                try:
                    cutoff = float(p.get('cutoff', '0.4'))
                except ValueError:
                    cutoff = 0.4
                try:
                    max_matches = int(p.get('max-matches', '20'))
                except ValueError:
                    max_matches = 20

                message = 'Searching for gallery matches locally in providers for wanted galleries.'
                frontend_logger.info(message)
                messages.success(request, message)

                matching_thread = threading.Thread(
                    name='web_search_worker',
                    target=create_matches_wanted_galleries_from_providers_internal,
                    args=(results, ),
                    kwargs={
                        'logger': frontend_logger, 'provider_filter': provider,
                        'cutoff': cutoff, 'max_matches': max_matches
                    }
                )
                matching_thread.daemon = True
                matching_thread.start()
            elif 'clear_all_matches' in p:
                GalleryMatch.objects.all().delete()
                message = 'Clearing matches from every wanted gallery.'
                frontend_logger.info(message)
                messages.success(request, message)

        params = {
        }

        for k, v in get.items():
            params[k] = v

        for k in wanted_gallery_filter_keys:
            if k not in params:
                params[k] = ''

        results = filter_wanted_galleries_simple(params)

        results = results.prefetch_related(
            Prefetch(
                'gallerymatch_set',
                queryset=GalleryMatch.objects.select_related('gallery', 'wanted_gallery').prefetch_related(
                    Prefetch(
                        'gallery__tags',
                        queryset=Tag.objects.filter(scope__exact='artist'),
                        to_attr='artist_tags'
                    )
                ),
                to_attr='possible_galleries'
            ),
            'possible_galleries__gallery__archive_set',
            'artists',
            'announces'
        ).order_by('-release_date')

        paginator = Paginator(results, 100)
        try:
            results = paginator.page(page)
        except (InvalidPage, EmptyPage):
            results = paginator.page(paginator.num_pages)

        matchers = crawler_settings.provider_context.get_matchers_name_priority(crawler_settings, matcher_type='title')

        d = {'results': results, 'title_matchers': matchers, 'form': form}
        return render(request, "viewer/wanted_galleries.html", d)
    else:
        return render_error(request, "Non existent list.")


def render_error(request, message):
    messages.error(request, message, extra_tags='danger')
    if 'HTTP_REFERER' in request.META:
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    else:
        return HttpResponseRedirect(reverse('viewer:main-page'))


def render_message(request, message):

    return render(
        request,
        'viewer/message.html',
        {"message": message}
    )


def about(request):

    return render(
        request,
        'viewer/about.html'
    )
