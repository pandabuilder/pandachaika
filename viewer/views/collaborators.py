from typing import List, Optional

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import permission_required, login_required
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render

from core.base.setup import Settings
from viewer.utils.actions import event_log
from viewer.forms import GallerySearchForm, ArchiveSearchForm
from viewer.models import Archive, Gallery, EventLog
from viewer.views.head import frontend_logger, gallery_filter_keys, filter_galleries_simple, \
    archive_filter_keys, filter_archives_simple

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
        if 'denied' in get:
            results = Gallery.objects.submitted_galleries(id__in=pks).order_by('-create_date')
        else:
            results = Gallery.objects.submitted_galleries(~Q(status=Gallery.DENIED), id__in=pks).order_by('-create_date')

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
                message = 'Queueing gallery: {}, link: {}, sourcelink: {}'.format(
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
                        gallery.reason = reason[:200]
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


@permission_required('viewer.publish_archive')
def publish_archives(request: HttpRequest) -> HttpResponse:
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
        archives = Archive.objects.filter(public=False, id__in=pks).order_by('-create_date')
        if 'publish_archives' in p:
            for archive in archives:
                message = 'Publishing archive: {}, link: {}'.format(
                    archive.title, archive.get_absolute_url()
                )
                if 'reason' in p and p['reason'] != '':
                    message += ', reason: {}'.format(p['reason'])
                frontend_logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)
                archive.set_public()
                event_log(
                    request.user,
                    'PUBLISH_ARCHIVE',
                    reason=p.get('reason', ''),
                    content_object=archive,
                    result='published'
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

    results = results.filter(
        public=False
    ).prefetch_related('gallery')

    paginator = Paginator(results, 100)
    try:
        results = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    d = {
        'results': results,
        'form': form
    }
    return render(request, "viewer/collaborators/publish_archives.html", d)


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
