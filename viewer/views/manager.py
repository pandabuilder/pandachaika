import threading
from itertools import groupby
from typing import List

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.db.models import Prefetch, Count, Q
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.conf import settings

from core.base.setup import Settings
from core.base.utilities import thread_exists
from viewer.forms import GallerySearchForm, ArchiveSearchForm, WantedGallerySearchForm
from viewer.models import Archive, Gallery, ArchiveMatches, Tag, WantedGallery, GalleryMatch, FoundGallery
from viewer.utils.actions import event_log
from viewer.utils.matching import generate_possible_matches_for_archives, \
    create_matches_wanted_galleries_from_providers, create_matches_wanted_galleries_from_providers_internal
from viewer.views.head import render_error, frontend_logger, gallery_filter_keys, filter_galleries_simple, \
    archive_filter_keys, filter_archives_simple, wanted_gallery_filter_keys, filter_wanted_galleries_simple

crawler_settings = settings.CRAWLER_SETTINGS


@staff_member_required(login_url='viewer:login')
def repeated_archives_for_galleries(request: HttpRequest) -> HttpResponse:
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
        pks: List[str] = []
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


@staff_member_required(login_url='viewer:login')
def repeated_archives_by_field(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", '')
    tags = get.get("tags", '')

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
        archives = Archive.objects.filter(id__in=pks).order_by('-create_date')

        user_reason = p.get('reason', '')

        if 'delete_archives' in p:
            for archive in archives:
                message = 'Removing archive: {} and deleting file: {}'.format(
                    archive.title, archive.zipped.path
                )
                frontend_logger.info(message)
                messages.success(request, message)

                gallery = archive.gallery
                archive.gallery.mark_as_deleted()
                archive.delete_all_files()
                archive.delete()

                event_log(
                    request.user,
                    'DELETE_ARCHIVE',
                    reason=user_reason,
                    content_object=gallery,
                    result='deleted'
                )

        elif 'delete_objects' in p:
            for archive in archives:
                message = 'Removing archive: {}, keeping file: {}'.format(
                    archive.title, archive.zipped.path
                )
                frontend_logger.info(message)
                messages.success(request, message)

                gallery = archive.gallery
                archive.gallery.mark_as_deleted()
                archive.delete_files_but_archive()
                archive.delete()

                event_log(
                    request.user,
                    'DELETE_ARCHIVE',
                    reason=user_reason,
                    content_object=gallery,
                    result='deleted'
                )

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

    if 'no-custom-tags' in get:
        results = results.annotate(num_custom_tags=Count('custom_tags')).filter(num_custom_tags=0)

    by_filesize = dict()
    by_crc32 = dict()

    for k, v in groupby(results.order_by('filesize'), lambda x: x.filesize):
        objects = list(v)
        if len(objects) > 1:
            by_filesize[k] = objects

    for k, v in groupby(results.order_by('crc32'), lambda x: x.crc32):
        objects = list(v)
        if len(objects) > 1:
            by_crc32[k] = objects

    # paginator = Paginator(results, 100)
    # try:
    #     results = paginator.page(page)
    # except (InvalidPage, EmptyPage):
    #     results = paginator.page(paginator.num_pages)

    d = {
        'by_filesize': by_filesize,
        'by_crc32': by_crc32,
        'form': form
    }
    return render(request, "viewer/archives_repeated_by_fields.html", d)


@staff_member_required(login_url='viewer:login')
def repeated_galleries_by_field(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", '')
    tags = get.get("tags", '')

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
                pks.append(v)
        results = Gallery.objects.filter(id__in=pks).order_by('-create_date')

        if 'delete_galleries' in p:

            user_reason = p.get('reason', '')

            for gallery in results:
                message = 'Removing gallery: {}, link: {}'.format(gallery.title, gallery.get_link())
                frontend_logger.info(message)
                messages.success(request, message)
                gallery.mark_as_deleted()

                event_log(
                    request.user,
                    'DELETE_GALLERY',
                    reason=user_reason,
                    content_object=gallery,
                    result='deleted'
                )

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

    results = results.eligible_for_use().exclude(title__exact='')

    if 'has-archives' in get:
        results = results.annotate(
            archives=Count('archive')
        ).filter(archives__gt=0)

    by_title = dict()

    if 'same-uploader' in get:
        for k, v in groupby(results.order_by('title', 'uploader'), lambda x: (x.title, x.uploader)):
            objects = list(v)
            if len(objects) > 1:
                by_title[k] = objects
    else:
        for k, v in groupby(results.order_by('title'), lambda x: x.title):
            objects = list(v)
            if len(objects) > 1:
                by_title[k] = objects

    providers = Gallery.objects.all().values_list('provider', flat=True).distinct()

    d = {
        'by_title': by_title,
        'form': form,
        'providers': providers
    }

    return render(request, "viewer/galleries_repeated_by_fields.html", d)


@staff_member_required(login_url='viewer:login')
def archive_filesize_different_from_gallery(request: HttpRequest) -> HttpResponse:
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
        pks: List[str] = []
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


def missing_archives_for_galleries(request: HttpRequest) -> HttpResponse:
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

                if current_settings.workers.web_queue:

                    current_settings.replace_metadata = True
                    current_settings.retry_failed = True

                    if 'reason' in p and p['reason'] != '':
                        reason = p['reason']
                        # Force limit string length (reason field max_length)
                        current_settings.archive_reason = reason[:200]
                        current_settings.archive_details = gallery.reason
                        current_settings.gallery_reason = reason[:200]
                    elif gallery.reason:
                        current_settings.archive_reason = gallery.reason

                    current_settings.workers.web_queue.enqueue_args_list(
                        (gallery.get_link(),),
                        override_options=current_settings
                    )
        elif 'recall_api' in p:
            message = 'Recalling API for {} galleries'.format(results.count())
            frontend_logger.info(message)
            messages.success(request, message)

            gallery_links = [x.get_link() for x in results]
            gallery_providers = list(results.values_list('provider', flat=True).distinct())

            current_settings = Settings(load_from_config=crawler_settings.config)

            if current_settings.workers.web_queue:
                current_settings.set_update_metadata_options(providers=gallery_providers)

                current_settings.workers.web_queue.enqueue_args_list(gallery_links,
                                                                     override_options=current_settings)

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

        results = results.non_used_galleries().prefetch_related('foundgallery_set')

        paginator = Paginator(results, 50)
        try:
            results = paginator.page(page)
        except (InvalidPage, EmptyPage):
            results = paginator.page(paginator.num_pages)

        d = {'results': results, 'providers': providers, 'force_public': force_public, 'form': form}
    else:

        params = {
        }

        for k, v in get.items():
            params[k] = v

        for k in gallery_filter_keys:
            if k not in params:
                params[k] = ''

        results = filter_galleries_simple(params)

        results = results.non_used_galleries(public=True, provider__in=['panda', 'fakku'])
        d = {'results': results}
    return render(request, "viewer/archives_missing_for_galleries.html", d)


@staff_member_required(login_url='viewer:login')
def archives_not_present_in_filesystem(request: HttpRequest) -> HttpResponse:
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


@staff_member_required(login_url='viewer:login')
def archives_not_matched_with_gallery(request: HttpRequest) -> HttpResponse:
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
                archive.delete_all_files()
                archive.delete()
        elif 'delete_objects' in p:
            for archive in archives:
                message = 'Removing archive not matched: {}, keeping file: {}'.format(
                    archive.title, archive.zipped.path
                )
                frontend_logger.info(message)
                messages.success(request, message)
                archive.delete_files_but_archive()
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
                max_matches = int(p.get('max-matches', '10'))
            except ValueError:
                max_matches = 10

            web_match_thread = threading.Thread(
                name='web_match_worker',
                target=generate_possible_matches_for_archives,
                args=(archives,),
                kwargs={
                    'logger': frontend_logger, 'cutoff': cutoff, 'max_matches': max_matches,
                    'filters': (matcher_filter,),
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
                max_matches = int(p.get('max-matches', '10'))
            except ValueError:
                max_matches = 10

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

    paginator = Paginator(results, 100)
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


def wanted_galleries(request: HttpRequest) -> HttpResponse:
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
            Q(should_search=True)
            & Q(found=False)
            & Q(public=True)
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

            try:
                cutoff = float(p.get('cutoff', '0.4'))
            except ValueError:
                cutoff = 0.4
            try:
                max_matches = int(p.get('max-matches', '10'))
            except ValueError:
                max_matches = 10

            message = 'Searching for gallery matches in providers for wanted galleries.'
            frontend_logger.info(message)
            messages.success(request, message)

            panda_search_thread = threading.Thread(
                name='web_search_worker',
                target=create_matches_wanted_galleries_from_providers,
                args=(results, provider),
                kwargs={
                    'logger': frontend_logger,
                    'cutoff': cutoff, 'max_matches': max_matches,
                })
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
                max_matches = int(p.get('max-matches', '10'))
            except ValueError:
                max_matches = 10

            try:
                must_be_used = bool(p.get('must-be-used', False))
            except ValueError:
                must_be_used = False

            message = 'Searching for gallery matches locally in providers for wanted galleries.'
            frontend_logger.info(message)
            messages.success(request, message)

            matching_thread = threading.Thread(
                name='web_search_worker',
                target=create_matches_wanted_galleries_from_providers_internal,
                args=(results,),
                kwargs={
                    'logger': frontend_logger, 'provider_filter': provider,
                    'cutoff': cutoff, 'max_matches': max_matches,
                    'must_be_used': must_be_used
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


def found_galleries(request: HttpRequest) -> HttpResponse:

    get = request.GET

    title = get.get("title", '')
    tags = get.get("tags", '')

    try:
        page = int(get.get("page", '1'))
    except ValueError:
        page = 1

    if not request.user.is_staff:
        results = FoundGallery.objects.filter(
            wanted_gallery__public=True,
            gallery__public=True
        ).prefetch_related(
            'wanted_gallery',
            'gallery'
        ).order_by('-create_date')

        paginator = Paginator(results, 100)
        try:
            results = paginator.page(page)
        except (InvalidPage, EmptyPage):
            results = paginator.page(paginator.num_pages)

        return render(request, "viewer/found_galleries.html", {'results': results})

    if 'clear' in get:
        form = WantedGallerySearchForm()
    else:
        form = WantedGallerySearchForm(initial={'title': title, 'tags': tags})

    params = {
    }

    for k, v in get.items():
        params[k] = v

    for k in wanted_gallery_filter_keys:
        if k not in params:
            params[k] = ''

    wanted_galleries_results = filter_wanted_galleries_simple(params)

    results = FoundGallery.objects.filter(
        wanted_gallery__in=wanted_galleries_results
    ).prefetch_related(
        'wanted_gallery',
        'gallery'
    ).order_by('-create_date')

    paginator = Paginator(results, 100)
    try:
        results = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    d = {'results': results, 'form': form}
    return render(request, "viewer/found_galleries.html", d)
