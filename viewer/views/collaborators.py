import threading
from typing import List, Optional

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import permission_required, login_required
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.db.models import Q, Prefetch, Count, Case, When
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, Http404
from django.shortcuts import render
from django.urls import reverse

from core.base.setup import Settings
from core.base.utilities import thread_exists
from viewer.utils.matching import generate_possible_matches_for_archives
from viewer.utils.actions import event_log
from viewer.forms import GallerySearchForm, ArchiveSearchForm, WantedGallerySearchForm, WantedGalleryCreateOrEditForm, \
    ArchiveCreateForm, ArchiveGroupSelectForm
from viewer.models import Archive, Gallery, EventLog, ArchiveMatches, Tag, WantedGallery, ArchiveGroup, \
    ArchiveGroupEntry
from viewer.utils.tags import sort_tags
from viewer.views.head import frontend_logger, gallery_filter_keys, filter_galleries_simple, \
    archive_filter_keys, filter_archives_simple, render_error, wanted_gallery_filter_keys, \
    filter_wanted_galleries_simple

crawler_settings = settings.CRAWLER_SETTINGS


@permission_required('viewer.approve_gallery')
def submit_queue(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", '')
    tags = get.get("tags", '')

    user_reason = p.get('reason', '')

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
            if k.startswith("sel-"):
                # k, pk = k.split('-')
                # results[pk][k] = v
                pks.append(v)

        preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])

        if 'denied' in get:
            results = Gallery.objects.submitted_galleries(id__in=pks).order_by(preserved)
        else:
            results = Gallery.objects.submitted_galleries(~Q(status=Gallery.DENIED), id__in=pks).order_by(preserved)

        if 'deny_galleries' in p:
            for gallery in results:
                message = 'Denying gallery: {}, link: {}, source link: {}'.format(
                    gallery.title, gallery.get_absolute_url(), gallery.get_link()
                )
                if 'reason' in p and p['reason'] != '':
                    message += ', reason: {}'.format(p['reason'])
                frontend_logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)
                gallery.mark_as_denied()
                event_log(
                    request.user,
                    'DENY_GALLERY',
                    reason=user_reason,
                    content_object=gallery,
                    result='denied'
                )
        elif 'download_galleries' in p:
            for gallery in results:
                message = 'Queueing gallery: {}, link: {}, source link: {}'.format(
                    gallery.title, gallery.get_absolute_url(), gallery.get_link()
                )
                if 'reason' in p and p['reason'] != '':
                    message += ', reason: {}'.format(p['reason'])
                frontend_logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)

                event_log(
                    request.user,
                    'ACCEPT_GALLERY',
                    reason=user_reason,
                    content_object=gallery,
                    result='accepted'
                )

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

                    def archive_callback(x: Optional['Archive'], crawled_url: Optional[str], result: str) -> None:
                        event_log(
                            request.user,
                            'ADD_ARCHIVE',
                            reason=user_reason,
                            content_object=x,
                            result=result,
                            data=crawled_url
                        )

                    def gallery_callback(x: Optional['Gallery'], crawled_url: Optional[str], result: str) -> None:
                        event_log(
                            request.user,
                            'ADD_GALLERY',
                            reason=user_reason,
                            content_object=x,
                            result=result,
                            data=crawled_url
                        )

                    current_settings.workers.web_queue.enqueue_args_list(
                        (gallery.get_link(),),
                        override_options=current_settings,
                        archive_callback=archive_callback,
                        gallery_callback=gallery_callback,
                    )

    providers = Gallery.objects.all().values_list('provider', flat=True).distinct()

    params = {
    }

    for k, v in get.items():
        params[k] = v

    for k in gallery_filter_keys:
        if k not in params:
            params[k] = ''

    results = filter_galleries_simple(params)

    if 'denied' in get:
        results = results.submitted_galleries().prefetch_related('foundgallery_set')
    else:
        results = results.submitted_galleries(~Q(status=Gallery.DENIED)).prefetch_related('foundgallery_set')

    paginator = Paginator(results, 50)
    try:
        results = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    d = {'results': results, 'providers': providers, 'form': form}
    return render(request, "viewer/collaborators/submit_queue.html", d)


@permission_required('viewer.manage_archive')
def manage_archives(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", '')
    tags = get.get("tags", '')

    user_reason = p.get('reason', '')

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

        preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])

        archives = Archive.objects.filter(id__in=pks).order_by(preserved)
        if 'publish_archives' in p and request.user.has_perm('viewer.publish_archive'):
            for archive in archives:
                message = 'Publishing archive: {}, link: {}'.format(
                    archive.title, archive.get_absolute_url()
                )
                if 'reason' in p and p['reason'] != '':
                    message += ', reason: {}'.format(p['reason'])
                frontend_logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)
                archive.set_public(reason=user_reason)
                event_log(
                    request.user,
                    'PUBLISH_ARCHIVE',
                    reason=user_reason,
                    content_object=archive,
                    result='published'
                )
        elif 'unpublish_archives' in p and request.user.has_perm('viewer.publish_archive'):
            for archive in archives:
                message = 'Unpublishing archive: {}, link: {}'.format(
                    archive.title, archive.get_absolute_url()
                )
                if 'reason' in p and p['reason'] != '':
                    message += ', reason: {}'.format(p['reason'])
                frontend_logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)
                archive.set_private(reason=user_reason)
                event_log(
                    request.user,
                    'UNPUBLISH_ARCHIVE',
                    reason=user_reason,
                    content_object=archive,
                    result='unpublished'
                )
        elif 'delete_archives' in p and request.user.has_perm('viewer.delete_archive'):
            for archive in archives:
                message = 'Deleting archive: {}, link: {}, with it\'s file: {} and associated gallery: {}'.format(
                    archive.title, archive.get_absolute_url(),
                    archive.zipped.path, archive.gallery
                )
                if 'reason' in p and p['reason'] != '':
                    message += ', reason: {}'.format(p['reason'])
                frontend_logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)
                gallery = archive.gallery
                archive.gallery.mark_as_deleted()
                archive.gallery = None
                archive.delete_all_files()
                archive.delete()
                event_log(
                    request.user,
                    'DELETE_ARCHIVE',
                    content_object=gallery,
                    reason=user_reason,
                    result='deleted'
                )
        elif 'update_metadata' in p and request.user.has_perm('viewer.update_metadata'):
            for archive in archives:
                gallery = archive.gallery

                message = 'Updating gallery API data for gallery: {} and related archives'.format(
                    gallery.get_absolute_url()
                )
                if 'reason' in p and p['reason'] != '':
                    message += ', reason: {}'.format(p['reason'])
                frontend_logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)

                current_settings = Settings(load_from_config=crawler_settings.config)

                if current_settings.workers.web_queue:
                    current_settings.set_update_metadata_options(providers=(gallery.provider,))

                    def gallery_callback(x: Optional['Gallery'], crawled_url: Optional[str], result: str) -> None:
                        event_log(
                            request.user,
                            'UPDATE_METADATA',
                            reason=user_reason,
                            content_object=x,
                            result=result,
                            data=crawled_url
                        )

                    current_settings.workers.web_queue.enqueue_args_list(
                        (gallery.get_link(),),
                        override_options=current_settings,
                        gallery_callback=gallery_callback
                    )

                    frontend_logger.info(
                        'Updating gallery API data for gallery: {} and related archives'.format(
                            gallery.get_absolute_url()
                        )
                    )
        elif 'add_to_group' in p and request.user.has_perm('viewer.change_archivegroup'):

            if 'archive_group' in p:
                archive_group_ids = p.getlist('archive_group')

                preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(archive_group_ids)])

                archive_groups = ArchiveGroup.objects.filter(pk__in=archive_group_ids).order_by(preserved)

                for archive in archives:
                    for archive_group in archive_groups:
                        if not ArchiveGroupEntry.objects.filter(archive=archive, archive_group=archive_group).exists():

                            archive_group_entry = ArchiveGroupEntry(archive=archive, archive_group=archive_group)
                            archive_group_entry.save()

                            message = 'Adding archive: {}, link: {}, to group: {}, link {}'.format(
                                archive.title, archive.get_absolute_url(),
                                archive_group.title, archive_group.get_absolute_url()
                            )
                            if 'reason' in p and p['reason'] != '':
                                message += ', reason: {}'.format(p['reason'])
                            frontend_logger.info("User {}: {}".format(request.user.username, message))
                            messages.success(request, message)
                            event_log(
                                request.user,
                                'ADD_ARCHIVE_TO_GROUP',
                                content_object=archive,
                                reason=user_reason,
                                result='added'
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

    results = results.prefetch_related('gallery')

    paginator = Paginator(results, 100)
    try:
        results = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    d = {
        'results': results,
        'form': form
    }

    if request.user.has_perm('viewer.change_archivegroup'):
        group_form = ArchiveGroupSelectForm()
        d.update(group_form=group_form)

    return render(request, "viewer/collaborators/manage_archives.html", d)


@login_required
def my_event_log(request: HttpRequest) -> HttpResponse:
    get = request.GET

    try:
        page = int(get.get("page", '1'))
    except ValueError:
        page = 1

    results = EventLog.objects.filter(user=request.user)

    paginator = Paginator(results, 100)
    try:
        results = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    d = {
        'results': results,
    }
    return render(request, "viewer/collaborators/event_log.html", d)


@permission_required('viewer.read_all_logs')
def users_event_log(request: HttpRequest) -> HttpResponse:
    get = request.GET

    try:
        page = int(get.get("page", '1'))
    except ValueError:
        page = 1

    results = EventLog.objects.all()

    paginator = Paginator(results, 100)
    try:
        results = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    d = {
        'results': results,
    }
    return render(request, "viewer/collaborators/user_event_log.html", d)


@permission_required('viewer.crawler_adder')
def user_crawler(request: HttpRequest) -> HttpResponse:
    """Crawl given URLs."""

    d = {}

    p = request.POST

    all_downloaders = crawler_settings.provider_context.get_downloaders_name_priority(
        crawler_settings, filter_name='generic_'
    )

    # providers_not_generic = list(set([x[0].provider for x in all_downloaders if not x[0].provider.is_generic()]))
    generic_downloaders = [x[0] for x in all_downloaders]

    user_reason = p.get('reason', '')

    if p:
        current_settings = Settings(load_from_config=crawler_settings.config)
        if not current_settings.workers.web_queue:
            messages.error(request, 'Cannot submit links currently. Please contact an admin.')
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
        url_set = set()
        # create dictionary of properties for each archive
        current_settings.replace_metadata = False
        current_settings.config['allowed']['replace_metadata'] = 'no'
        for k, v in p.items():
            if k == "downloader":
                if v == 'no-generic':
                    continue
                elif v in generic_downloaders:
                    current_settings.enable_downloader_only(v)
            elif k == "urls":
                url_list = v.split("\n")
                for item in url_list:
                    url_set.add(item.rstrip('\r'))
        urls = list(url_set)

        if not urls:
            messages.error(request, 'Submission is empty.')
            return HttpResponseRedirect(request.META["HTTP_REFERER"])

        if 'reason' in p and p['reason'] != '':
            reason = p['reason']
            # Force limit string length (reason field max_length)
            current_settings.archive_reason = reason[:200]
            current_settings.gallery_reason = reason[:200]
        if 'source' in p and p['source'] != '':
            source = p['source']
            # Force limit string length (reason field max_length)
            current_settings.archive_source = source[:50]

        current_settings.archive_user = request.user

        parsers = crawler_settings.provider_context.get_parsers_classes()

        def archive_callback(x: Optional['Archive'], crawled_url: Optional[str], result: str) -> None:
            event_log(
                request.user,
                'ADD_ARCHIVE',
                reason=user_reason,
                content_object=x,
                result=result,
                data=crawled_url
            )

        def gallery_callback(x: Optional['Gallery'], crawled_url: Optional[str], result: str) -> None:
            event_log(
                request.user,
                'ADD_GALLERY',
                reason=user_reason,
                content_object=x,
                result=result,
                data=crawled_url
            )
        current_settings.workers.web_queue.enqueue_args_list(
            urls,
            override_options=current_settings,
            archive_callback=archive_callback,
            gallery_callback=gallery_callback,
            use_argparser=False
        )

        messages.success(
            request,
            'Starting Crawler, if the links were correctly added, they should appear on the archive or gallery list.'
        )
        for url in urls:
            frontend_logger.info("User {}: queued link: {}".format(request.user.username, url))
            # event_log(
            #     request.user,
            #     'CRAWL_URL',
            #     reason=user_reason,
            #     data=url,
            #     result='queue'
            # )

        found_valid_urls: List[str] = []

        for parser in parsers:
            if parser.id_from_url_implemented():
                urls_filtered = parser.filter_accepted_urls(urls)
                found_valid_urls.extend(urls_filtered)
                for url_filtered in urls_filtered:
                    gid = parser.id_from_url(url_filtered)
                    gallery = Gallery.objects.filter(gid=gid).first()
                    if not gallery:
                        messages.success(
                            request,
                            '{}: New URL, will be added to the submit queue'.format(url_filtered)
                        )
                        event_log(
                            request.user,
                            'CRAWL_URL',
                            reason=user_reason,
                            data=url_filtered,
                            result='queued'
                        )
                        continue
                    if gallery.is_submitted():
                        messages.info(
                            request,
                            '{}: Already in submit queue, link: {}, reason: {}'.format(
                                url_filtered, gallery.get_absolute_url(), gallery.reason
                            )
                        )
                        event_log(
                            request.user,
                            'CRAWL_URL',
                            reason=user_reason,
                            data=url_filtered,
                            result='already_submitted'
                        )
                    elif gallery.public:
                        messages.info(
                            request,
                            '{}: Already present, is public: {}'.format(
                                url_filtered,
                                request.build_absolute_uri(gallery.get_absolute_url())
                            )
                        )
                        event_log(
                            request.user,
                            'CRAWL_URL',
                            reason=user_reason,
                            data=url_filtered,
                            result='already_public'
                        )
                    else:
                        messages.info(
                            request,
                            '{}: Already present, is not public: {}'.format(
                                url_filtered,
                                request.build_absolute_uri(gallery.get_absolute_url())
                            )
                        )
                        event_log(
                            request.user,
                            'CRAWL_URL',
                            reason=user_reason,
                            data=url_filtered,
                            result='already_private'
                        )

        extra_urls = [x for x in urls if x not in found_valid_urls]

        for extra_url in extra_urls:
            messages.info(
                request,
                '{}: Extra non-provider URLs'.format(
                    extra_url
                )
            )
            event_log(
                request.user,
                'CRAWL_URL',
                reason=user_reason,
                data=extra_url,
                result='queued'
            )
        # Not really optimal when there's many commands being queued
        # for command in url_list:
        #     messages.success(request, command)
        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    d.update({
        'downloaders': generic_downloaders
    })

    return render(request, "viewer/collaborators/gallery_crawler.html", d)


@permission_required('viewer.match_archive')
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

        preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])

        archives = Archive.objects.filter(id__in=pks).order_by(preserved)
        if 'create_possible_matches' in p:
            if thread_exists('match_unmatched_worker'):
                return render_error(request, "Local matching worker is already running.")
            provider = p['create_possible_matches']
            try:
                cutoff = float(p.get('cutoff', '0.4'))
            except ValueError:
                cutoff = 0.4
            try:
                max_matches = int(p.get('max-matches', '10'))
            except ValueError:
                max_matches = 10

            frontend_logger.info(
                'User {}: Looking for possible matches in gallery database '
                'for non-matched archives (cutoff: {}, max matches: {}) '
                'using provider filter "{}"'.format(request.user.username, cutoff, max_matches, provider)
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
        elif 'clear_possible_matches' in p:

            for archive in archives:
                archive.possible_matches.clear()

            frontend_logger.info(
                'User {}: Clearing possible matches for archives'.format(request.user.username)
            )
            messages.success(request, 'Clearing possible matches.')

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

    if 'show-matched' not in get:
        results = results.filter(gallery__isnull=True)

    results = results.prefetch_related(
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

    if 'with-possible-matches' in get:
        results = results.annotate(n_possible_matches=Count('possible_matches')).filter(n_possible_matches__gt=0)

    paginator = Paginator(results, 50)
    try:
        results = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    d = {
        'results': results,
        'providers': Gallery.objects.all().values_list('provider', flat=True).distinct(),
        'form': form
    }
    return render(request, "viewer/collaborators/unmatched_archives.html", d)


@login_required
def archive_update(request: HttpRequest, pk: int, tool: str = None, tool_use_id: str = None) -> HttpResponse:
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if tool == 'select-as-match' and request.user.has_perm('viewer.match_archive'):
        archive.select_as_match(tool_use_id)
        if archive.gallery:
            frontend_logger.info("User: {}: Archive {} ({}) was matched with gallery {} ({}).".format(
                request.user.username,
                archive,
                reverse('viewer:archive', args=(archive.pk,)),
                archive.gallery,
                reverse('viewer:gallery', args=(archive.gallery.pk,)),
            ))
            event_log(
                request.user,
                'MATCH_ARCHIVE',
                # reason=user_reason,
                data=reverse('viewer:gallery', args=(archive.gallery.pk,)),
                content_object=archive,
                result='matched'
            )
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == 'clear-possible-matches' and request.user.has_perm('viewer.match_archive'):
        archive.possible_matches.clear()
        frontend_logger.info("User: {}: Archive {} ({}) was cleared from its possible matches.".format(
            request.user.username,
            archive,
            reverse('viewer:archive', args=(archive.pk,)),
        ))
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    else:
        return render_error(request, 'Unrecognized command')


@permission_required('viewer.view_wantedgallery')
def wanted_galleries(request: HttpRequest) -> HttpResponse:
    # p = request.POST
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

    if request.POST.get('submit-wanted-gallery') and request.user.has_perm('viewer.add_wantedgallery'):
        # create a form instance and populate it with data from the request:
        edit_form = WantedGalleryCreateOrEditForm(request.POST)
        # check whether it's valid:
        if edit_form.is_valid():
            new_wanted_gallery = edit_form.save()
            message = 'New wanted gallery successfully created'
            messages.success(request, message)
            frontend_logger.info("User {}: {}".format(request.user.username, message))
            event_log(
                request.user,
                'ADD_WANTED_GALLERY',
                content_object=new_wanted_gallery,
                result='created'
            )
        else:
            messages.error(request, 'The provided data is not valid', extra_tags='danger')
            # return HttpResponseRedirect(request.META["HTTP_REFERER"])
    else:
        edit_form = WantedGalleryCreateOrEditForm()

    params = {
    }

    for k, v in get.items():
        params[k] = v

    for k in wanted_gallery_filter_keys:
        if k not in params:
            params[k] = ''

    results = filter_wanted_galleries_simple(params)

    results = results.prefetch_related(
        # Prefetch(
        #     'gallerymatch_set',
        #     queryset=GalleryMatch.objects.select_related('gallery', 'wanted_gallery').prefetch_related(
        #         Prefetch(
        #             'gallery__tags',
        #             queryset=Tag.objects.filter(scope__exact='artist'),
        #             to_attr='artist_tags'
        #         )
        #     ),
        #     to_attr='possible_galleries'
        # ),
        # 'possible_galleries__gallery__archive_set',
        'artists',
        'mentions'
    ).order_by('-release_date')

    paginator = Paginator(results, 100)
    try:
        results = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    d = {'results': results, 'form': form, 'edit_form': edit_form}
    return render(request, "viewer/collaborators/wanted_galleries.html", d)


@permission_required('viewer.view_wantedgallery')
def wanted_gallery(request: HttpRequest, pk: int) -> HttpResponse:
    """WantedGallery listing."""
    try:
        wanted_gallery_instance = WantedGallery.objects.get(pk=pk)
    except WantedGallery.DoesNotExist:
        raise Http404("Wanted gallery does not exist")

    if request.POST.get('submit-wanted-gallery') and request.user.has_perm('viewer.change_wantedgallery'):
        # create a form instance and populate it with data from the request:
        edit_form = WantedGalleryCreateOrEditForm(request.POST, instance=wanted_gallery_instance)
        # check whether it's valid:
        if edit_form.is_valid():
            new_wanted_gallery = edit_form.save()
            message = 'Wanted gallery successfully modified'
            messages.success(request, message)
            frontend_logger.info("User {}: {}".format(request.user.username, message))
            event_log(
                request.user,
                'CHANGE_WANTED_GALLERY',
                content_object=new_wanted_gallery,
                result='changed'
            )
            # return HttpResponseRedirect(request.META["HTTP_REFERER"])
        else:
            messages.error(request, 'The provided data is not valid', extra_tags='danger')
            # return HttpResponseRedirect(request.META["HTTP_REFERER"])
    else:
        edit_form = WantedGalleryCreateOrEditForm(instance=wanted_gallery_instance)

    wanted_tag_lists = sort_tags(wanted_gallery_instance.wanted_tags.all())
    unwanted_tag_lists = sort_tags(wanted_gallery_instance.unwanted_tags.all())

    d = {
        'wanted_gallery': wanted_gallery_instance,
        'wanted_tag_lists': wanted_tag_lists,
        'unwanted_tag_lists': unwanted_tag_lists,
        'edit_form': edit_form
    }
    return render(request, "viewer/collaborators/wanted_gallery.html", d)


@permission_required('viewer.upload_with_metadata_archive')
def upload_archive(request: HttpRequest) -> HttpResponse:

    if request.POST.get('submit-archive'):
        # create a form instance and populate it with data from the request:
        edit_form = ArchiveCreateForm(request.POST, request.FILES)
        # check whether it's valid:
        if edit_form.is_valid():
            new_archive = edit_form.save(commit=False)
            new_archive.user = request.user
            new_archive = edit_form.save()
            message = 'Archive successfully uploaded'
            messages.success(request, message)
            frontend_logger.info("User {}: {}".format(request.user.username, message))
            event_log(
                request.user,
                'ADD_ARCHIVE',
                content_object=new_archive,
                result='added'
            )
            # return HttpResponseRedirect(request.META["HTTP_REFERER"])
        else:
            messages.error(request, 'The provided data is not valid', extra_tags='danger')
            # return HttpResponseRedirect(request.META["HTTP_REFERER"])
    else:
        edit_form = ArchiveCreateForm()

    d = {
        'edit_form': edit_form
    }
    return render(request, "viewer/collaborators/add_archive.html", d)
