# This file is intended for experimental views
# (data visualizations, panda react viewer, etc.)

import json
import re
from itertools import chain
import typing

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import serializers
from django.db import transaction
from django.db.models import Q, QuerySet
from django.http import HttpResponse, HttpRequest, Http404
from django.http.request import QueryDict
from django.shortcuts import render
from django.core.paginator import Paginator, EmptyPage
from django.urls import reverse
from django.conf import settings

from core.base.types import DataDict
from core.base.utilities import timestamp_or_zero, str_to_int
from viewer.forms import GallerySearchForm, SpanErrorList, GallerySearchSimpleForm
from viewer.models import Gallery, Tag, Archive, Image, UserArchivePrefs
from viewer.views.head import archive_filter_keys, filter_archives, render_error
from viewer.views.api import simple_archive_filter

crawler_settings = settings.CRAWLER_SETTINGS


# @login_required
def tag_frequency(request: HttpRequest) -> HttpResponse:
    if not crawler_settings.urls.enable_tag_frequency:
        if not request.user.is_staff:
            raise Http404("Page not found")
        else:
            return render_error(request, "Page disabled by settings (urls: enable_tag_frequency).")
    title = request.GET.get("title", '')
    tags = request.GET.get("tags", '')
    if 'clear' in request.GET:
        request.GET = QueryDict('')
        form = GallerySearchForm()
        form_simple = GallerySearchSimpleForm(request.GET, error_class=SpanErrorList)
    else:
        form = GallerySearchForm(initial={'title': title, 'tags': tags})
        form_simple = GallerySearchSimpleForm(request.GET, error_class=SpanErrorList)
        form_simple.is_valid()
        for field_name, errors in form_simple.errors.items():
            messages.error(request, field_name + ": " + ", ".join(errors), extra_tags='danger')
    d = {'form': form, 'form_simple': form_simple}
    return render(request, "viewer/graph_tags.html", d)


def gallery_frequency(request: HttpRequest) -> HttpResponse:
    if not crawler_settings.urls.enable_gallery_frequency:
        if not request.user.is_staff:
            raise Http404("Page not found")
        else:
            return render_error(request, "Page disabled by settings (urls: enable_gallery_frequency).")
    title = request.GET.get("title", '')
    tags = request.GET.get("tags", '')
    if 'clear' in request.GET:
        request.GET = QueryDict('')
        form = GallerySearchForm()
        form_simple = GallerySearchSimpleForm(request.GET, error_class=SpanErrorList)
    else:
        form = GallerySearchForm(initial={'title': title, 'tags': tags})
        form_simple = GallerySearchSimpleForm(request.GET, error_class=SpanErrorList)
        form_simple.is_valid()
        for field_name, errors in form_simple.errors.items():
            messages.error(request, field_name + ": " + ", ".join(errors), extra_tags='danger')
    d = {'form': form, 'form_simple': form_simple}
    return render(request, "viewer/graph_gallery_posted.html", d)


def get_gallery_data(data: QueryDict) -> 'QuerySet[Gallery]':
    """Quick search of galleries.
    """

    # sort and filter results by parameters
    order = '-posted'

    results = Gallery.objects.order_by(order)

    if 'title' in data:
        q_formatted = '%' + data['title'].replace(' ', '%') + '%'
        results = results.filter(
            Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
        )

    if 'tags' in data:
        tags = data['tags'].split(',')
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

    if 'filecount_from' in data:
        results = results.filter(filecount__gte=int(float(data['filecount_from'])))
    if 'filecount_to' in data:
        results = results.filter(filecount__lte=int(float(data['filecount_to'])))
    if 'filesize_from' in data:
        results = results.filter(filesize__gte=int(float(data['filesize_from'])))
    if 'filesize_to' in data:
        results = results.filter(filesize__lte=int(float(data['filesize_to'])))
    if 'posted_from' in data:
        results = results.filter(posted__gte=data['posted_from'])
    if 'posted_to' in data:
        results = results.filter(posted__lte=data['posted_to'])
    if 'provider' in data:
        results = results.filter(provider__icontains=data['provider'])
    if 'reason' in data:
        results = results.filter(reason__icontains=data['reason'])
    if 'provider' in data:
        results = results.filter(provider__icontains=data['provider'])
    if 'uploader' in data:
        results = results.filter(uploader__icontains=data['uploader'])

    results = results.distinct()

    return results


def seeder(request: HttpRequest) -> HttpResponse:
    if not crawler_settings.urls.enable_tag_frequency:
        if not request.user.is_staff:
            response = json.dumps({'error': "Page not Found"})
        else:
            response = json.dumps({'error': "Page disabled by settings (urls: enable_tag_frequency)."})
        return HttpResponse(response, content_type="application/json")
    if request.method == 'GET':
        data = request.GET
        galleries = get_gallery_data(data).filter(posted__gt='1970-01-01 00:00:00+00:00')
        tags = Tag.objects.filter(gallery__in=galleries).distinct()
        combined = list(chain(tags, galleries))
        response = serializers.serialize("json", combined, fields=('title', 'name', 'scope', 'pk', 'tags'))
    else:
        response = json.dumps({'error': "method must be GET"})
    return HttpResponse(response, content_type="application/json")


def release_date_seeder(request: HttpRequest) -> HttpResponse:
    if not crawler_settings.urls.enable_gallery_frequency:
        if not request.user.is_staff:
            response = json.dumps({'error': "Page not Found"})
        else:
            response = json.dumps({'error': "Page disabled by settings (urls: enable_gallery_frequency)."})
        return HttpResponse(response, content_type="application/json")
    if request.method == 'GET':
        data = request.GET
        galleries = get_gallery_data(data).filter(posted__gt='1970-01-01 00:00:00+00:00')
        response = serializers.serialize("json", list(galleries), fields=('pk', 'title', 'posted'))
    else:
        response = json.dumps({'error': "method must be GET"})
    return HttpResponse(response, content_type="application/json")


def get_archive_data(data: QueryDict) -> 'QuerySet[Archive]':
    """Quick search of archives.
    """

    # sort and filter results by parameters
    order = 'create_date'
    if 'order' in data:
        order = data['order']
    if order == 'rating':
        order = 'gallery__' + order
    elif order == 'posted':
        order = 'gallery__' + order
    if 'asc' not in data:
        order = '-' + order

    results = Archive.objects.order_by(order)

    if 'title' in data:
        q_formatted = '%' + data['title'].replace(' ', '%') + '%'
        results = results.filter(
            Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
        )

    if 'filename' in data:
        results = results.filter(zipped__icontains=data['filename'])
    if 'rating_from' in data:
        results = results.filter(gallery__rating__gte=float(data['rating_from']))
    if 'rating_to' in data:
        results = results.filter(gallery__rating__lte=float(data['rating_to']))
    if 'filecount_from' in data:
        results = results.filter(filecount__gte=int(float(data['filecount_from'])))
    if 'filecount_to' in data:
        results = results.filter(filecount__lte=int(float(data['filecount_to'])))
    if 'filesize_from' in data:
        results = results.filter(filesize__gte=int(float(data['filesize_from'])))
    if 'filesize_to' in data:
        results = results.filter(filesize__lte=int(float(data['filesize_to'])))
    if 'posted_from' in data:
        results = results.filter(gallery__posted__gte=data['posted_from'])
    if 'posted_to' in data:
        results = results.filter(gallery__posted__lte=data['posted_to'])
    if 'match_type' in data:
        results = results.filter(match_type__icontains=data['match_type'])
    if 'source_type' in data:
        results = results.filter(source_type__icontains=data['source_type'])
    if 'extracted' in data:
        results = results.filter(extracted=True)

    # if 'term' in data:
    #     term = data['term'].split(',')
    #     for term in term:
    #         term = term.strip().replace(" ", "_")
    #         tag_clean = re.sub("^[-|^]", "", term)
    #         scope_name = tag_clean.split(":", maxsplit=1)
    #         if len(scope_name) > 1:
    #             tag_scope = scope_name[0]
    #             tag_name = scope_name[1]
    #         else:
    #             tag_scope = ''
    #             tag_name = scope_name[0]
    #         if term.startswith("-"):
    #             results = results.exclude(
    #                 Q(tags__name__contains=tag_name),
    #                 Q(tags__scope__contains=tag_scope))
    #         elif term.startswith("^"):
    #             results = results.filter(
    #                 Q(tags__name__exact=tag_name),
    #                 Q(tags__scope__exact=tag_scope))
    #         else:
    #             results = results.filter(
    #                 Q(tags__name__contains=tag_name),
    #                 Q(tags__scope__contains=tag_scope))

    if 'q' in data:
        terms = data['q'].split()
        for term in terms:
            # q_formatted = '%' + term.strip().replace(' ', '%') + '%'
            # title_query = (
            #     Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
            # )
            tag = term.strip().replace(" ", "_")
            tag_clean = re.sub("^[-|^]", "", tag)
            scope_name = tag_clean.split(":", maxsplit=1)
            if term.startswith("-"):
                tag_query = (
                    Q(tags__name__contains=scope_name[1])
                    & Q(tags__scope__contains=scope_name[0])
                ) if len(scope_name) > 1 else Q(tags__name__contains=scope_name[0])
                results = results.exclude(
                    tag_query
                )
            elif term.startswith("^"):
                tag_query = (
                    Q(tags__name__exact=scope_name[1])
                    & Q(tags__scope__exact=scope_name[0])
                ) if len(scope_name) > 1 else Q(tags__name__exact=scope_name[0])
                results = results.filter(
                    tag_query
                )
            else:
                tag_query = (
                    Q(tags__name__contains=scope_name[1])
                    & Q(tags__scope__contains=scope_name[0])
                ) if len(scope_name) > 1 else Q(tags__name__contains=scope_name[0])
                results = results.filter(
                    tag_query
                )

    results = results.distinct().prefetch_related('tags')

    return results


# TODO: move modifying actions to POST requests. If something changes here, must update panda-react too.
def api(request: HttpRequest, model: str = None, obj_id: str = None, action: str = None) -> HttpResponse:
    if request.method == 'GET':
        data = request.GET
        if model == 'archive' and obj_id is not None:
            try:
                archive_id = int(obj_id)
            except (ValueError, TypeError):
                return HttpResponse(json.dumps({'result': "Archive does not exist."}), content_type="application/json; charset=utf-8")
            if action == 'extract_toggle':
                try:
                    if request.user.has_perm('viewer.expand_archive') or data.get('api_key', '') == crawler_settings.api_key:
                        with transaction.atomic():
                            archive = Archive.objects.select_for_update().get(pk=archive_id)
                            archive.extract_toggle()
                    else:
                        return HttpResponse(json.dumps({'result': "Admin only action."}),
                                            content_type="application/json; charset=utf-8")
                except Archive.DoesNotExist:
                    return HttpResponse(json.dumps({'result': "Archive does not exist."}),
                                        content_type="application/json; charset=utf-8")

                response = json.dumps({
                    'result': "ok",
                    'change': {
                        'field': 'extracted',
                        'value': archive.extracted,
                    },
                })
            elif action == 'extract_archive':
                try:
                    if request.user.has_perm('viewer.expand_archive') or data.get('api_key', '') == crawler_settings.api_key:
                        with transaction.atomic():
                            archive = Archive.objects.select_for_update().get(pk=archive_id)
                            if archive.extracted:
                                return HttpResponse(
                                    json.dumps({
                                        'result': "warning",
                                        'message': "Archive: {} is already extracted.".format(archive_id)
                                    }), content_type="application/json; charset=utf-8")
                            archive.extract()
                    else:
                        return HttpResponse(json.dumps({
                            'result': "error",
                            'message': "You don't have permission for this action."
                        }), content_type="application/json; charset=utf-8")
                except Archive.DoesNotExist:
                    return HttpResponse(json.dumps({
                        'result': "error",
                        'message': "Archive does not exist."
                    }), content_type="application/json; charset=utf-8")

                response = json.dumps({
                    'result': "ok",
                    'message': "Extraction successful.",
                    'change': {
                        'field': 'extracted',
                        'value': archive.extracted,
                    },
                })
            elif action == 'reduce_archive':
                try:
                    if request.user.has_perm('viewer.expand_archive') or data.get('api_key', '') == crawler_settings.api_key:
                        with transaction.atomic():
                            archive = Archive.objects.select_for_update().get(pk=archive_id)
                            if not archive.extracted:
                                return HttpResponse(
                                    json.dumps({
                                        'result': "warning",
                                        'message': "Archive: {} is already reduced.".format(archive_id)
                                    }), content_type="application/json; charset=utf-8")
                            archive.reduce()
                    else:
                        return HttpResponse(json.dumps({
                            'result': "error",
                            'message': "You don't have permission for this action."
                        }), content_type="application/json; charset=utf-8")
                except Archive.DoesNotExist:
                    return HttpResponse(json.dumps({
                        'result': "error",
                        'message': "Archive does not exist."
                    }), content_type="application/json; charset=utf-8")

                response = json.dumps({
                    'result': "ok",
                    'message': "Reduction successful.",
                    'change': {
                        'field': 'extracted',
                        'value': archive.extracted,
                    },
                })
            elif action == 'image_list':
                positions = data.getlist('i')
                try:
                    if request.user.is_authenticated or data.get('api_key', '') == crawler_settings.api_key:
                        images = Image.objects.filter(archive=archive_id, position__in=positions, extracted=True)
                    else:
                        images = Image.objects.filter(archive=archive_id, position__in=positions, extracted=True).filter(archive__public=True)
                except Archive.DoesNotExist:
                    return HttpResponse(json.dumps({'result': "Archive does not exist."}), content_type="application/json; charset=utf-8")
                image_urls: list[dict[str, typing.Any]] = [
                    {
                        'position': image.position,
                        'url': request.build_absolute_uri(image.image.url),
                        'is_horizontal': image.image_width / image.image_height > 1 if image.image_width and image.image_height else False,
                        'width': image.image_width,
                        'height': image.image_height
                    } for image in sorted(images, key=lambda img: positions.index(str(img.position)))
                ]
                response = json.dumps(image_urls)
            else:
                try:
                    if request.user.is_authenticated or data.get('api_key', '') == crawler_settings.api_key:
                        archive = Archive.objects.select_related('gallery').\
                            prefetch_related('tags').get(pk=archive_id)
                    else:
                        archive = Archive.objects.filter(public=True, extracted=True).select_related('gallery').\
                            prefetch_related('tags').get(pk=archive_id)
                except Archive.DoesNotExist:
                    return HttpResponse(json.dumps({'result': "Archive does not exist."}), content_type="application/json; charset=utf-8")
                position = int(data['position']) if 'position' in data else 1

                # We are sending 2 types for startImage in case it's not
                # extracted (None, object), this should be changed.
                response = json.dumps(
                    {
                        'id': archive.pk,
                        'title': archive.title,
                        'title_jpn': archive.title_jpn,
                        'category': archive.gallery.category if archive.gallery else '',
                        'uploader': archive.gallery.uploader if archive.gallery else '',
                        'posted': int(timestamp_or_zero(archive.gallery.posted)) if archive.gallery else '',
                        'filecount': archive.filecount,
                        'filesize': archive.filesize,
                        'download': request.build_absolute_uri(reverse('viewer:archive-download', args=(archive.pk,))),
                        'url': request.build_absolute_uri(reverse('viewer:archive', args=(archive.pk,))),
                        'expunged': archive.gallery.expunged if archive.gallery else '',
                        'rating': float(str_to_int(archive.gallery.rating)) if archive.gallery else '',
                        'fjord': archive.gallery.fjord if archive.gallery else None,
                        'tags': archive.tag_list_sorted(),
                        'extracted': archive.extracted,
                        'image': (request.build_absolute_uri(archive.thumbnail.url) if archive.thumbnail else None),
                        'lastPosition': int(archive.image_set.count()) or archive.filecount,
                        'startImage': archive.image_set.get(position=position).dump_image(request) if archive.extracted else None,
                    },
                    # indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
        elif model == 'archive':
            archives_list = get_archive_data(data)
            if not request.user.is_authenticated and not data.get('api_key', '') == crawler_settings.api_key:
                archives_list = archives_list.filter(public=True, extracted=True)
            if "favorites" in data and data["favorites"] and request.user.is_authenticated:
                user_arch_ids = UserArchivePrefs.objects.filter(
                    user=request.user.id, favorite_group=int(data["favorites"])).values_list('archive')
                archives_list = archives_list.filter(id__in=user_arch_ids)
            response = archives_to_json_response(archives_list, request)
        elif model == 'archives':

            display_prms: DataDict = {}

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

            for k, v in data.items():
                if k in parameters:
                    parameters[k] = v
                else:
                    display_prms[k] = v

            if 'view' not in parameters or parameters['view'] == '':
                parameters['view'] = 'list'
            if 'asc_desc' not in parameters or parameters['asc_desc'] == '':
                parameters['asc_desc'] = 'desc'
            if 'sort' not in parameters or parameters['sort'] == '':
                if request.user.is_authenticated or data.get('api_key', '') == crawler_settings.api_key:
                    parameters['sort'] = 'create_date'
                else:
                    parameters['sort'] = 'public_date'

            if data.get('api_key', '') == crawler_settings.api_key:
                force_private = True
            else:
                force_private = False

            archives_list_filtered: 'QuerySet[Archive]' = filter_archives(request, parameters, display_prms, force_private=force_private)
            if 'extracted' in data:
                archives_list_filtered = archives_list_filtered.filter(extracted=True)

            response = archives_to_json_response(archives_list_filtered.prefetch_related('tags', 'custom_tags'), request)
        elif model == 'archives_simple':
            q_args = data.get('q', '')
            if request.user.is_authenticated or data.get('api_key', '') == crawler_settings.api_key:
                results = simple_archive_filter(q_args, public=False)
            else:
                results = simple_archive_filter(q_args, public=True)
            response = json.dumps(
                [
                    {
                        'id': o.pk,
                        'title': o.best_title,
                        # 'tags': o.tag_list_sorted(),
                    } for o in results
                ]
            )
            return HttpResponse(response, content_type="application/json; charset=utf-8")
        elif model == 'me':
            response = json.dumps(
                {
                    'id': request.user.id,
                    'username': request.user.username,
                    'is_staff': request.user.is_staff,
                }
            )
        else:
            response = json.dumps({'result': "Unsupported operation"})
    else:
        response = json.dumps({'result': "Unsupported request method"})

    http_response = HttpResponse(response, content_type="application/json; charset=utf-8")
    return http_response


def archives_to_json_response(archives_list: 'QuerySet[Archive]', request: HttpRequest) -> str:
    try:
        archives_per_page = min(100, max(1, int(request.GET.get("limit", '48'))))
    except ValueError:
        archives_per_page = 48
    paginator = Paginator(archives_list, archives_per_page)
    try:
        page = int(request.GET.get("page", '1'))
    except ValueError:
        page = 1
    try:
        archives = paginator.page(page)
    except EmptyPage:
        # If page is out of range (e.g. 9999), deliver last page of results.
        archives = paginator.page(paginator.num_pages)
    response = json.dumps(
        {
            'objects': [{
                'id': archive.pk,
                'title': archive.title,
                'filecount': archive.filecount,
                'filesize': archive.filesize,
                'download': request.build_absolute_uri(reverse('viewer:archive-download', args=(archive.pk,))),
                'url': request.build_absolute_uri(reverse('viewer:archive', args=(archive.pk,))),
                'image': (request.build_absolute_uri(archive.thumbnail.url) if archive.thumbnail else None),
                'image_height': (archive.thumbnail_height if archive.thumbnail else None),
                'image_width': (archive.thumbnail_width if archive.thumbnail else None),
                'tags': archive.tag_list_sorted(),
                'extracted': archive.extracted,
            } for archive in archives
            ],
            'has_previous': archives.has_previous(),
            'has_next': archives.has_next(),
            'num_pages': paginator.num_pages,
            'count': paginator.count,
            'number': archives.number,
        }
    )
    return response


@login_required
def new_image_viewer(request: HttpRequest, archive: int = None, image: int = None) -> HttpResponse:

    return render(request, "viewer/new_image_viewer.html")
