# This file is intended for views designed to interact via JSON with
# other tools (userscript, happypanda, etc.)

import json
import re

import logging
from typing import Dict, Any, List

from django.core.paginator import Paginator, EmptyPage
from django.db.models import Q
from django.http import HttpResponse, HttpRequest
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings

from core.base.setup import Settings
from core.base.utilities import str_to_int
from core.base.utilities import timestamp_or_zero
from viewer.models import Archive, Gallery, ArchiveQuerySet, GalleryQuerySet
from viewer.utils.matching import generate_possible_matches_for_archives
from viewer.views.head import gallery_filter_keys, gallery_order_fields

crawler_logger = logging.getLogger('viewer.webcrawler')
crawler_settings = settings.CRAWLER_SETTINGS


# NOTE: This is used by 3rd parties, do not modify, at most create a new function if something needs changing
# Public API, does not check for any token, but filters if the user is authenticated or not.
@csrf_exempt
def json_search(request: HttpRequest) -> HttpResponse:

    if request.method == 'GET':
        data = request.GET
        # Get fields from a specific archive.
        if 'archive' in data:
            try:
                archive_id = int(data['archive'])
            except ValueError:
                return HttpResponse(json.dumps({'result': "Archive does not exist."}), content_type="application/json; charset=utf-8")
            try:
                archive = Archive.objects.get(pk=archive_id)
            except Archive.DoesNotExist:
                return HttpResponse(json.dumps({'result': "Archive does not exist."}), content_type="application/json; charset=utf-8")
            if not archive.public and not request.user.is_authenticated:
                return HttpResponse(json.dumps({'result': "Archive does not exist."}), content_type="application/json; charset=utf-8")
            response = json.dumps(
                {
                    'title': archive.title,
                    'title_jpn': archive.title_jpn,
                    'category': archive.gallery.category if archive.gallery else '',
                    'uploader': archive.gallery.uploader if archive.gallery else '',
                    'posted': int(timestamp_or_zero(archive.gallery.posted)) if archive.gallery else '',
                    'filecount': archive.filecount,
                    'filesize': archive.filesize,
                    'expunged': archive.gallery.expunged if archive.gallery else '',
                    'rating': float(str_to_int(archive.gallery.rating)) if archive.gallery else '',
                    'fjord': archive.gallery.fjord if archive.gallery else '',
                    'tags': archive.tag_list(),
                    'download': reverse('viewer:archive-download', args=(archive.pk,)),
                    'gallery': archive.gallery.pk if archive.gallery else '',
                },
                # indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            return HttpResponse(response, content_type="application/json; charset=utf-8")
        # Get tags from a specific archive.
        elif 'at' in data:
            try:
                archive_id = int(data['at'])
            except ValueError:
                return HttpResponse(json.dumps({'result': "Archive does not exist."}), content_type="application/json; charset=utf-8")
            try:
                archive = Archive.objects.get(pk=archive_id)
            except Archive.DoesNotExist:
                return HttpResponse(json.dumps({'result': "Archive does not exist."}), content_type="application/json; charset=utf-8")
            if not archive.public and not request.user.is_authenticated:
                return HttpResponse(json.dumps({'result': "Archive does not exist."}), content_type="application/json; charset=utf-8")
            response = json.dumps(
                {
                    'tags': archive.tag_list_sorted(),
                },
                # indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            return HttpResponse(response, content_type="application/json; charset=utf-8")
        # Get fields from a specific gallery.
        elif 'gallery' in data:
            try:
                gallery_id = int(data['gallery'])
            except ValueError:
                return HttpResponse(json.dumps({'result': "Gallery does not exist."}), content_type="application/json; charset=utf-8")
            try:
                gallery = Gallery.objects.get(pk=gallery_id)
            except Gallery.DoesNotExist:
                return HttpResponse(json.dumps({'result': "Gallery does not exist."}), content_type="application/json; charset=utf-8")
            if not gallery.public and not request.user.is_authenticated:
                return HttpResponse(json.dumps({'result': "Gallery does not exist."}), content_type="application/json; charset=utf-8")
            response = json.dumps(
                {
                    'title': gallery.title,
                    'title_jpn': gallery.title_jpn,
                    'category': gallery.category,
                    'uploader': gallery.uploader,
                    'posted': int(timestamp_or_zero(gallery.posted)),
                    'filecount': gallery.filecount,
                    'filesize': gallery.filesize,
                    'expunged': gallery.expunged,
                    'rating': float(str_to_int(gallery.rating)),
                    'fjord': gallery.fjord,
                    'tags': gallery.tag_list(),
                    'archives': [{'id': archive.id, 'download': reverse('viewer:archive-download', args=(archive.pk,))} for archive in gallery.archive_set.all()],
                },
                # indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            return HttpResponse(response, content_type="application/json; charset=utf-8")
        # Get fields from several archives by one of it's images sha1 value.
        elif 'sha1' in data:
            archives = Archive.objects.filter(image__sha1=data['sha1'])
            if not archives:
                return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")
            if not request.user.is_authenticated:
                archives = archives.filter(public=True)
            if not archives:
                return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")
            response = json.dumps(
                [{
                    'id': archive.id,
                    'title': archive.title,
                    'title_jpn': archive.title_jpn,
                    'category': archive.gallery.category if archive.gallery else '',
                    'uploader': archive.gallery.uploader if archive.gallery else '',
                    'posted': int(timestamp_or_zero(archive.gallery.posted)) if archive.gallery else '',
                    'filecount': archive.filecount,
                    'filesize': archive.filesize,
                    'expunged': archive.gallery.expunged if archive.gallery else '',
                    'rating': float(str_to_int(archive.gallery.rating)) if archive.gallery else '',
                    'fjord': archive.gallery.fjord if archive.gallery else '',
                    'tags': archive.tag_list(),
                    'download': reverse('viewer:archive-download', args=(archive.pk,)),
                    'gallery': archive.gallery.pk if archive.gallery else '',
                } for archive in archives],
                # indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            return HttpResponse(response, content_type="application/json; charset=utf-8")
        # Get reduced number of fields from several archives by doing a simple filtering.
        elif 'q' in data:
            args = data['q']
            if not request.user.is_authenticated:
                results = simple_archive_filter(args, public=True)
            else:
                results = simple_archive_filter(args, public=False)
            response = json.dumps(
                [{
                    'id': o.pk,
                    'title': o.title,
                    'tags': o.tag_list(),
                    'url': reverse('viewer:archive-download', args=(o.pk,))} for o in results
                 ]
            )
            return HttpResponse(response, content_type="application/json; charset=utf-8")
        # Get fields from several galleries by doing a simple filtering.
        elif 'g' in data:

            args = data.copy()

            for k in gallery_filter_keys:
                if k not in args:
                    args[k] = ''

            keys = ("sort", "asc_desc")

            for k in keys:
                if k not in args:
                    args[k] = ''

            # args = data
            if not request.user.is_authenticated:
                args['public'] = True
            else:
                args['public'] = False
            results = filter_galleries_no_request(args)
            if not results:
                return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")
            response = json.dumps(
                [{
                    'title': gallery.title,
                    'title_jpn': gallery.title_jpn,
                    'category': gallery.category,
                    'uploader': gallery.uploader,
                    'posted': int(timestamp_or_zero(gallery.posted)),
                    'filecount': gallery.filecount,
                    'filesize': gallery.filesize,
                    'expunged': gallery.expunged,
                    'source': gallery.provider,
                    'rating': float(str_to_int(gallery.rating)),
                    'fjord': gallery.fjord,
                    'tags': gallery.tag_list(),
                } for gallery in results
                ],
                # indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            return HttpResponse(response, content_type="application/json; charset=utf-8")
        # this part should be used in conjunction with json crawler provider, to transfer easily already fetch links.
        # Get more fields from several galleries by doing a simple filtering.
        elif 'gc' in data:

            args = data.copy()

            for k in gallery_filter_keys:
                if k not in args:
                    args[k] = ''

            keys = ("sort", "asc_desc")

            for k in keys:
                if k not in args:
                    args[k] = ''

            # args = data
            if not request.user.is_authenticated:
                args['public'] = True
            else:
                args['public'] = False
            results = filter_galleries_no_request(args)
            if not results:
                return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")
            response = json.dumps(
                [{
                    'gid': gallery.gid,
                    'token': gallery.token,
                    'title': gallery.title,
                    'title_jpn': gallery.title_jpn,
                    'category': gallery.category,
                    'uploader': gallery.uploader,
                    'posted': int(timestamp_or_zero(gallery.posted)),
                    'filecount': gallery.filecount,
                    'filesize': gallery.filesize,
                    'expunged': gallery.expunged,
                    'provider': gallery.provider,
                    'rating': gallery.rating,
                    'fjord': gallery.fjord,
                    'tags': gallery.tag_list(),
                    'link': gallery.get_link()
                } for gallery in results
                ],
                # indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            return HttpResponse(response, content_type="application/json; charset=utf-8")
        # this part should be used in conjunction with json crawler provider, to transfer easily already fetch links.
        # Get more fields from several galleries by doing a simple filtering.
        # More complete version of the last one, since you also get the archives (DL link only, use the gallery data
        # to create the final archive).
        # Gallery search, no pagination
        elif 'gs' in data:

            args = data.copy()

            for k in gallery_filter_keys:
                if k not in args:
                    args[k] = ''

            keys = ("sort", "asc_desc")

            for k in keys:
                if k not in args:
                    args[k] = ''

            # args = data
            if not request.user.is_authenticated:
                args['public'] = True
            else:
                args['public'] = False
            results = filter_galleries_no_request(args)
            if not results:
                return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")
            response = json.dumps(
                gallery_search_results_to_json(request, results),
                # indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            return HttpResponse(response, content_type="application/json; charset=utf-8")
        # Gallery search with pagination
        elif 'gsp' in data:

            args = data.copy()

            for k in gallery_filter_keys:
                if k not in args:
                    args[k] = ''

            keys = ("sort", "asc_desc")

            for k in keys:
                if k not in args:
                    args[k] = ''

            # args = data
            if not request.user.is_authenticated:
                args['public'] = True
            else:
                args['public'] = False
            results = filter_galleries_no_request(args)

            paginator = Paginator(results, 48)
            try:
                page = int(args.get("page", '1'))
            except ValueError:
                page = 1
            try:
                results = paginator.page(page)
            except EmptyPage:
                # If page is out of range (e.g. 9999), deliver last page of results.
                results = paginator.page(paginator.num_pages)

            response = json.dumps(
                {
                    'galleries': gallery_search_results_to_json(request, results),
                    'has_previous': results.has_previous(),
                    'has_next': results.has_next(),
                    'num_pages': paginator.num_pages,
                    'count': paginator.count,
                    'number': results.number,
                },
                # indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            return HttpResponse(response, content_type="application/json; charset=utf-8")
        # Gallery data
        elif 'gd' in data:
            try:
                gallery_id = int(data['gd'])
            except ValueError:
                return HttpResponse(json.dumps({'result': "Gallery does not exist."}), content_type="application/json; charset=utf-8")
            try:
                gallery = Gallery.objects.get(pk=gallery_id)
            except Gallery.DoesNotExist:
                return HttpResponse(json.dumps({'result': "Gallery does not exist."}), content_type="application/json; charset=utf-8")
            if not gallery.public and not request.user.is_authenticated:
                return HttpResponse(json.dumps({'result': "Gallery does not exist."}), content_type="application/json; charset=utf-8")
            response = json.dumps(
                {
                    'id': gallery.pk,
                    'gid': gallery.gid,
                    'token': gallery.token,
                    'title': gallery.title,
                    'title_jpn': gallery.title_jpn,
                    'category': gallery.category,
                    'uploader': gallery.uploader,
                    'comment': gallery.comment,
                    'posted': int(timestamp_or_zero(gallery.posted)),
                    'filecount': gallery.filecount,
                    'filesize': gallery.filesize,
                    'expunged': gallery.expunged,
                    'provider': gallery.provider,
                    'rating': gallery.rating,
                    'fjord': gallery.fjord,
                    'tags': gallery.tag_list(),
                    'link': gallery.get_link(),
                    'thumbnail': request.build_absolute_uri(
                        reverse('viewer:gallery-thumb', args=(gallery.pk,))) if gallery.thumbnail else '',
                    'thumbnail_url': gallery.thumbnail_url,
                    'archives': [
                        request.build_absolute_uri(
                            reverse('viewer:archive-download', args=(archive.pk,))
                        ) for archive in gallery.archive_set.all()
                    ],
                },
                # indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            return HttpResponse(response, content_type="application/json; charset=utf-8")
        else:
            return HttpResponse(json.dumps({'result': "Wrong command"}), content_type="application/json; charset=utf-8")
    else:
        return HttpResponse(json.dumps({'result': "Request must be GET"}), content_type="application/json; charset=utf-8")


def gallery_search_results_to_json(request: HttpRequest, galleries: GalleryQuerySet) -> List[Dict[str, Any]]:
    return [{
        'id': gallery.pk,
        'gid': gallery.gid,
        'token': gallery.token,
        'title': gallery.title,
        'title_jpn': gallery.title_jpn,
        'category': gallery.category,
        'uploader': gallery.uploader,
        'comment': gallery.comment,
        'posted': int(timestamp_or_zero(gallery.posted)),
        'filecount': gallery.filecount,
        'filesize': gallery.filesize,
        'expunged': gallery.expunged,
        'provider': gallery.provider,
        'rating': gallery.rating,
        'fjord': gallery.fjord,
        'tags': gallery.tag_list(),
        'link': gallery.get_link(),
        'thumbnail': request.build_absolute_uri(
            reverse('viewer:gallery-thumb', args=(gallery.pk,))) if gallery.thumbnail else '',
        'thumbnail_url': gallery.thumbnail_url,
        'archives': [
            {
                'link': request.build_absolute_uri(
                    reverse('viewer:archive-download', args=(archive.pk,))
                ),
                'source': archive.source_type,
                'reason': archive.reason
            } for archive in gallery.archive_set.all()
        ],
    } for gallery in galleries
    ]


# Private API, checks for the API key.
@csrf_exempt
def json_parser(request: HttpRequest) -> HttpResponse:
    response = {}

    if request.method == 'POST':
        if not request.body:
            response['error'] = 'Empty request'
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
        data = json.loads(request.body.decode("utf-8"))
        if 'api_key' not in data:
            response['error'] = 'Missing API key'
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
        elif data['api_key'] != crawler_settings.api_key:
            response['error'] = 'Incorrect API key'
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
        # send some 'ok' back
        else:
            if 'operation' not in data or 'args' not in data:
                response['error'] = 'Wrong format'
            else:
                args = data['args']
                response = {}
                # Used by internal pages and userscript
                if data['operation'] == 'webcrawler' and 'link' in args:
                    if not crawler_settings.workers.web_queue:
                        response['error'] = 'The webqueue is not running'
                    elif 'downloader' in args:
                        current_settings = Settings(load_from_config=crawler_settings.config)
                        if not current_settings.workers.web_queue:
                            response['error'] = 'The webqueue is not running'
                        else:
                            current_settings.allow_downloaders_only([args['downloader']], True, True, True)
                            archive = None
                            parsers = current_settings.provider_context.get_parsers(current_settings, crawler_logger)
                            for parser in parsers:
                                if parser.id_from_url_implemented():
                                    urls_filtered = parser.filter_accepted_urls((args['link'], ))
                                    for url_filtered in urls_filtered:
                                        gallery_gid = parser.id_from_url(url_filtered)
                                        if gallery_gid:
                                            archive = Archive.objects.filter(gallery__gid=gallery_gid).first()
                                    if urls_filtered:
                                        break
                            current_settings.workers.web_queue.enqueue_args_list((args['link'],), override_options=current_settings)
                            if archive:
                                response['message'] = "Archive exists, crawling to check for redownload: " + args['link']
                            else:
                                response['message'] = "Crawling: " + args['link']
                    else:
                        if 'parentLink' in args:
                            parent_archive = None
                            parsers = crawler_settings.provider_context.get_parsers(crawler_settings, crawler_logger)
                            for parser in parsers:
                                if parser.id_from_url_implemented():
                                    urls_filtered = parser.filter_accepted_urls((args['parentLink'],))
                                    for url_filtered in urls_filtered:
                                        gallery_gid = parser.id_from_url(url_filtered)
                                        if gallery_gid:
                                            parent_archive = Archive.objects.filter(gallery__gid=gallery_gid).first()
                                    if urls_filtered:
                                        break
                            if parent_archive:
                                link = parent_archive.gallery.get_link()
                                if 'action' in args and args['action'] == 'replaceFound':
                                    parent_archive.gallery.mark_as_deleted()
                                    parent_archive.gallery = None
                                    parent_archive.delete_all_files()
                                    parent_archive.delete_files_but_archive()
                                    parent_archive.delete()
                                    response['message'] = "Crawling: " + args['link'] + ", deleting parent: " + link
                                    crawler_settings.workers.web_queue.enqueue_args(args['link'])
                                elif 'action' in args and args['action'] == 'queueFound':
                                    response['message'] = "Crawling: " + args['link'] + ", keeping parent: " + link
                                    crawler_settings.workers.web_queue.enqueue_args(args['link'])
                                else:
                                    response['message'] = "Please confirm deletion of parent: " + link
                                    response['action'] = 'confirmDeletion'
                            else:
                                archive = None
                                parsers = crawler_settings.provider_context.get_parsers(crawler_settings, crawler_logger)
                                for parser in parsers:
                                    if parser.id_from_url_implemented():
                                        urls_filtered = parser.filter_accepted_urls((args['link'],))
                                        for url_filtered in urls_filtered:
                                            gallery_gid = parser.id_from_url(url_filtered)
                                            if gallery_gid:
                                                archive = Archive.objects.filter(gallery__gid=gallery_gid).first()
                                        if urls_filtered:
                                            break
                                if archive:
                                    response['message'] = "Archive exists, crawling to check for redownload: " + args['link']
                                else:
                                    response['message'] = "Crawling: " + args['link']
                                crawler_settings.workers.web_queue.enqueue_args(args['link'])
                        else:
                            archive = None
                            parsers = crawler_settings.provider_context.get_parsers(crawler_settings, crawler_logger)
                            for parser in parsers:
                                if parser.id_from_url_implemented():
                                    urls_filtered = parser.filter_accepted_urls((args['link'],))
                                    for url_filtered in urls_filtered:
                                        gallery_gid = parser.id_from_url(url_filtered)
                                        if gallery_gid:
                                            archive = Archive.objects.filter(gallery__gid=gallery_gid).first()
                                    if urls_filtered:
                                        break
                            if archive:
                                response['message'] = "Archive exists, crawling to check for redownload: " + args['link']
                            else:
                                response['message'] = "Crawling: " + args['link']
                            crawler_settings.workers.web_queue.enqueue_args(args['link'])
                    if not response:
                        response['error'] = 'Could not parse request'
                    return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
                # Used by remotesite command
                elif data['operation'] == 'archive_request':
                    archives_query = Archive.objects.filter_non_existent(crawler_settings.MEDIA_ROOT, gallery__gid__in=args)
                    archives = [{'gid': archive.gallery.gid,
                                 'id': archive.id,
                                 'zipped': archive.zipped.name,
                                 'filesize': archive.filesize} for archive in archives_query]
                    response_text = json.dumps({'result': archives})
                    return HttpResponse(response_text, content_type="application/json; charset=utf-8")
                # Used by remotesite command
                elif data['operation'] in ('queue_archives', 'queue_galleries'):
                    urls = args
                    new_urls_set = set()
                    gids_set = set()

                    parsers = crawler_settings.provider_context.get_parsers(crawler_settings, crawler_logger)
                    for parser in parsers:
                        if parser.id_from_url_implemented():
                            urls_filtered = parser.filter_accepted_urls(urls)
                            for url in urls_filtered:
                                gid = parser.id_from_url(url)
                                gids_set.add(gid)

                    gids_list = list(gids_set)

                    existing_galleries = Gallery.objects.filter(gid__in=gids_list)
                    for gallery_object in existing_galleries:
                        if gallery_object.is_submitted():
                            gallery_object.delete()
                        # Delete queue galleries that failed, and does not have archives.
                        elif data['operation'] == 'queue_archives' and "failed" in gallery_object.dl_type and not gallery_object.archive_set.all():
                            gallery_object.delete()
                        elif data['operation'] == 'queue_archives' and not gallery_object.archive_set.all():
                            gallery_object.delete()
                    already_present_gids = list(Gallery.objects.filter(gid__in=gids_list).values_list('gid', flat=True))
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
                        if data['operation'] == 'queue_galleries':
                            current_settings.allow_type_downloaders_only('info')
                        elif data['operation'] == 'queue_archives':
                            if 'archive_reason' in data:
                                current_settings.archive_reason = data['archive_reason']
                            if 'archive_details' in data:
                                current_settings.archive_details = data['archive_details']
                            current_settings.allow_type_downloaders_only('fake')
                        if current_settings.workers.web_queue:
                            current_settings.workers.web_queue.enqueue_args_list(pages_links, override_options=current_settings)
                        else:
                            pages_links = []
                    return HttpResponse(json.dumps({'result': str(len(pages_links))}), content_type="application/json; charset=utf-8")
                # Used by remotesite command
                elif data['operation'] == 'links':
                    links = args
                    if len(links) > 0:
                        crawler_settings.workers.web_queue.enqueue_args_list(links)
                    return HttpResponse(json.dumps({'result': str(len(links))}), content_type="application/json; charset=utf-8")
                # Used by archive page
                elif data['operation'] == 'match_archive':
                    archive = Archive.objects.filter(pk=args['archive'])
                    if archive:
                        generate_possible_matches_for_archives(
                            archive,
                            filters=(args['match_filter'],),
                            logger=crawler_logger,
                            match_local=False,
                            match_web=True,
                        )
                    return HttpResponse(json.dumps({'message': 'web matcher done, check the logs for results'}),
                                        content_type="application/json; charset=utf-8")
                elif data['operation'] == 'match_archive_internally':
                    archive = Archive.objects.get(pk=args['archive'])
                    if archive:
                        clear_title = True if 'clear' in args else False
                        provider_filter = args.get('provider', '')
                        try:
                            cutoff = float(request.GET.get('cutoff', '0.4'))
                        except ValueError:
                            cutoff = 0.4
                        try:
                            max_matches = int(request.GET.get('max-matches', '10'))
                        except ValueError:
                            max_matches = 10

                        archive.generate_possible_matches(
                            clear_title=clear_title, provider_filter=provider_filter,
                            cutoff=cutoff, max_matches=max_matches
                        )
                        archive.save()
                    return HttpResponse(json.dumps({'message': 'internal matcher done, check the archive for results'}),
                                        content_type="application/json; charset=utf-8")
                else:
                    response['error'] = 'Unknown function'
    elif request.method == 'GET':
        data = request.GET
        if 'api_key' not in data:
            response['error'] = 'Missing API key'
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
        elif data['api_key'] != crawler_settings.api_key:
            response['error'] = 'Incorrect API key'
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
        # send some 'ok' back
        else:
            if 'gc' in data:
                args = data.copy()

                for k in gallery_filter_keys:
                    if k not in args:
                        args[k] = ''

                keys = ("sort", "asc_desc")

                for k in keys:
                    if k not in args:
                        args[k] = ''

                # args = data
                # Already authorized by api key.
                args['public'] = False

                results = filter_galleries_no_request(args)
                if not results:
                    return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")
                response_text = json.dumps(
                    [{
                        'gid': gallery.gid,
                        'token': gallery.token,
                        'title': gallery.title,
                        'title_jpn': gallery.title_jpn,
                        'category': gallery.category,
                        'uploader': gallery.uploader,
                        'comment': gallery.comment,
                        'posted': int(timestamp_or_zero(gallery.posted)),
                        'filecount': gallery.filecount,
                        'filesize': gallery.filesize,
                        'expunged': gallery.expunged,
                        'rating': gallery.rating,
                        'hidden': gallery.hidden,
                        'fjord': gallery.fjord,
                        'public': gallery.public,
                        'provider': gallery.provider,
                        'dl_type': gallery.dl_type,
                        'tags': gallery.tag_list(),
                        'link': gallery.get_link(),
                        'thumbnail': request.build_absolute_uri(reverse('viewer:gallery-thumb', args=(gallery.pk,))) if gallery.thumbnail else '',
                        'thumbnail_url': gallery.thumbnail_url
                    } for gallery in results
                    ],
                    # indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
                return HttpResponse(response_text, content_type="application/json; charset=utf-8")
            else:
                response['error'] = 'Unknown function'
    else:
        response['error'] = 'Unsupported method: {}'.format(request.method)
    return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")


def simple_archive_filter(args: str, public: bool = True) -> ArchiveQuerySet:
    """Simple filtering of archives.
    """

    # sort and filter results by parameters
    order = '-gallery__posted'

    if public:
        results = Archive.objects.order_by(order).filter(public=True)
    else:
        results = Archive.objects.order_by(order)

    q_formatted = '%' + args.replace(' ', '%') + '%'
    results_title = results.filter(
        Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
    )

    tags = args.split(',')
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
                (Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope))
                | (Q(custom_tags__name__contains=tag_name) & Q(custom_tags__scope__contains=tag_scope))
            )
        elif tag.startswith("^"):
            results = results.filter(
                (Q(tags__name__exact=tag_name) & Q(tags__scope__exact=tag_scope))
                | (Q(custom_tags__name__exact=tag_name) & Q(custom_tags__scope__exact=tag_scope))
            )
        else:
            results = results.filter(
                (Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope))
                | (Q(custom_tags__name__contains=tag_name) & Q(custom_tags__scope__contains=tag_scope))
            )
    results = results | results_title

    results = results.distinct()

    return results


def filter_galleries_no_request(filter_args: Dict[str, Any]) -> GalleryQuerySet:

    # sort and filter results by parameters
    order = "posted"
    if filter_args["sort"] and filter_args["sort"] in gallery_order_fields:
        order = filter_args["sort"]
    if filter_args["asc_desc"] == "desc":
        order = '-' + order

    results = Gallery.objects.eligible_for_use().order_by(order)

    if filter_args["public"]:
        results = results.filter(public=bool(filter_args["public"]))

    if filter_args["title"]:
        q_formatted = '%' + filter_args["title"].replace(' ', '%') + '%'
        results = results.filter(
            Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
        )
    if filter_args["rating_from"]:
        results = results.filter(rating__gte=float(filter_args["rating_from"]))
    if filter_args["rating_to"]:
        results = results.filter(rating__lte=float(filter_args["rating_to"]))
    if filter_args["filecount_from"]:
        results = results.filter(filecount__gte=int(filter_args["filecount_from"]))
    if filter_args["filecount_to"]:
        results = results.filter(filecount__lte=int(filter_args["filecount_to"]))
    if filter_args["filesize_from"]:
        results = results.filter(filesize__gte=float(filter_args["filesize_from"]))
    if filter_args["filesize_to"]:
        results = results.filter(filesize__lte=float(filter_args["filesize_to"]))
    if filter_args["posted_from"]:
        results = results.filter(posted__gte=filter_args["posted_from"])
    if filter_args["posted_to"]:
        results = results.filter(posted__lte=filter_args["posted_to"])
    if filter_args["create_from"]:
        results = results.filter(create_date__gte=filter_args["create_from"])
    if filter_args["create_to"]:
        results = results.filter(create_date__lte=filter_args["create_to"])
    if filter_args["category"]:
        results = results.filter(category__icontains=filter_args["category"])
    if filter_args["expunged"]:
        results = results.filter(expunged=filter_args["expunged"])
    if filter_args["hidden"]:
        results = results.filter(hidden=filter_args["hidden"])
    if filter_args["fjord"]:
        results = results.filter(fjord=filter_args["fjord"])
    if filter_args["uploader"]:
        results = results.filter(uploader=filter_args["uploader"])
    if filter_args["provider"]:
        results = results.filter(provider=filter_args["provider"])
    if filter_args["dl_type"]:
        results = results.filter(dl_type=filter_args["dl_type"])

    if filter_args["tags"]:
        tags = filter_args["tags"].split(',')
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
