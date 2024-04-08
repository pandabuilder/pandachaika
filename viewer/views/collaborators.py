import json
import logging
import re
import threading
from collections import defaultdict
from itertools import groupby
from typing import Optional

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import permission_required, login_required
from django.contrib.contenttypes.models import ContentType
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.db.models import Prefetch, Count, Case, When, QuerySet, Q, F
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, Http404, QueryDict
from django.shortcuts import render
from django.urls import reverse
from django.utils.crypto import get_random_string

from core.base.setup import Settings
from core.base.utilities import thread_exists, clamp, get_schedulers_status
from viewer.utils.functions import archive_manage_results_to_json, galleries_update_metadata
from viewer.utils.matching import generate_possible_matches_for_archives
from viewer.utils.actions import event_log
from viewer.forms import GallerySearchForm, ArchiveSearchForm, WantedGalleryCreateOrEditForm, \
    ArchiveCreateForm, ArchiveGroupSelectForm, GalleryCreateForm, ArchiveManageEntrySimpleForm, \
    WantedGalleryColSearchForm, ArchiveManageSearchSimpleForm, AllGalleriesSearchForm, \
    EventSearchForm
from viewer.models import Archive, Gallery, EventLog, ArchiveMatches, Tag, WantedGallery, ArchiveGroup, \
    ArchiveGroupEntry, GallerySubmitEntry, MonitoredLink, ArchiveRecycleEntry, ArchiveTag, UserLongLivedToken
from viewer.utils.tags import sort_tags
from viewer.utils.types import AuthenticatedHttpRequest
from viewer.views.head import gallery_filter_keys, filter_galleries_simple, \
    archive_filter_keys, filter_archives_simple, render_error, wanted_gallery_filter_keys, \
    filter_wanted_galleries_simple

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)


@permission_required('viewer.view_submitted_gallery')
def submit_queue(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", '')
    tags = get.get("tags", '')

    user_reason = p.get('reason', '')
    entry_reason = p.get('entry_reason', '')
    entry_comment = p.get('entry_comment', '')

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

        gallery_entries = GallerySubmitEntry.objects.filter(id__in=pks).order_by(preserved)

        if 'deny_galleries' in p and request.user.has_perm('viewer.approve_gallery'):
            for gallery_entry in gallery_entries:
                gallery = gallery_entry.gallery
                if gallery:
                    message = 'Denying gallery: {}, link: {}, source link: {}'.format(
                        gallery.title, gallery.get_absolute_url(), gallery.get_link()
                    )
                    if entry_reason != '':
                        message += ', reason: {}'.format(entry_reason)
                    logger.info("User {}: {}".format(request.user.username, message))
                    messages.success(request, message)
                    gallery.mark_as_denied()
                    gallery_entry.mark_as_denied(reason=entry_reason, comment=entry_comment)
                    event_log(
                        request.user,
                        'DENY_GALLERY',
                        reason=entry_reason,
                        content_object=gallery,
                        result='denied'
                    )
                else:
                    message = 'Denying URL: {}, '.format(
                        gallery_entry.submit_url
                    )
                    if 'reason' in p and p['reason'] != '':
                        message += ', reason: {}'.format(p['reason'])
                    logger.info("User {}: {}".format(request.user.username, message))
                    messages.success(request, message)

                    gallery_entry.mark_as_denied(reason=entry_reason, comment=entry_comment)

                    event_log(
                        request.user,
                        'DENY_URL',
                        reason=entry_reason,
                        content_object=gallery_entry,
                        result='denied'
                    )
        elif 'publish_galleries' in p and request.user.has_perm('viewer.publish_gallery'):
            for gallery_entry in gallery_entries:
                gallery = gallery_entry.gallery
                if gallery and not gallery.public:
                    message = 'Publishing gallery and related archives: {}, link: {}, source link: {}'.format(
                        gallery.title, gallery.get_absolute_url(), gallery.get_link()
                    )
                    if 'reason' in p and p['reason'] != '':
                        message += ', reason: {}'.format(p['reason'])
                    logger.info("User {}: {}".format(request.user.username, message))
                    messages.success(request, message)
                    gallery.set_public()
                    event_log(
                        request.user,
                        'PUBLISH_GALLERY',
                        reason=entry_reason,
                        content_object=gallery,
                        result='success'
                    )
                    if request.user.has_perm('viewer.publish_archive'):
                        for archive in gallery.archive_set.all():
                            if not archive.public:
                                archive.set_public()
                                event_log(
                                    request.user,
                                    'PUBLISH_ARCHIVE',
                                    reason=entry_reason,
                                    content_object=archive,
                                    result='success'
                                )
        elif 'approve_galleries' in p and request.user.has_perm('viewer.approve_gallery'):
            for gallery_entry in gallery_entries:
                gallery = gallery_entry.gallery
                if gallery:
                    message = 'Approving gallery: {}, link: {}, source link: {}'.format(
                        gallery.title, gallery.get_absolute_url(), gallery.get_link()
                    )
                    if 'reason' in p and p['reason'] != '':
                        message += ', reason: {}'.format(p['reason'])
                    logger.info("User {}: {}".format(request.user.username, message))
                    messages.success(request, message)

                    if user_reason:
                        gallery.reason = user_reason
                        gallery.save()
                    gallery_entry.mark_as_approved(reason=entry_reason, comment=entry_comment)

                    event_log(
                        request.user,
                        'APPROVE_GALLERY',
                        reason=entry_reason,
                        content_object=gallery,
                        result='accepted'
                    )
                else:
                    message = 'Approving URL: {}, '.format(
                        gallery_entry.submit_url
                    )
                    if 'reason' in p and p['reason'] != '':
                        message += ', reason: {}'.format(p['reason'])
                    logger.info("User {}: {}".format(request.user.username, message))
                    messages.success(request, message)

                    gallery_entry.mark_as_approved(reason=entry_reason, comment=entry_comment)

                    event_log(
                        request.user,
                        'APPROVE_URL',
                        reason=entry_reason,
                        content_object=gallery_entry,
                        result='accepted'
                    )

        elif 'download_galleries' in p and request.user.has_perm('viewer.approve_gallery'):
            for gallery_entry in gallery_entries:
                gallery = gallery_entry.gallery
                if gallery:
                    message = 'Queueing gallery: {}, link: {}, source link: {}'.format(
                        gallery.title, gallery.get_absolute_url(), gallery.get_link()
                    )
                    if 'reason' in p and p['reason'] != '':
                        message += ', reason: {}'.format(p['reason'])
                    logger.info("User {}: {}".format(request.user.username, message))
                    messages.success(request, message)

                    gallery_entry.mark_as_approved(reason=entry_reason, comment=entry_comment)

                    # Force replace_metadata when queueing from this list, since it's mostly used to download non used.
                    current_settings = Settings(load_from_config=crawler_settings.config)

                    if current_settings.workers.web_queue:

                        current_settings.replace_metadata = True
                        current_settings.retry_failed = True
                        if request.user.is_authenticated:
                            current_settings.archive_user = request.user
                        current_settings.archive_origin = Archive.ORIGIN_ACCEPT_SUBMITTED

                        if 'reason' in p and p['reason'] != '':
                            reason = p['reason']
                            # Force limit string length (reason field max_length)
                            current_settings.archive_reason = reason[:200]
                            current_settings.archive_details = gallery.reason or ''
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

                    event_log(
                        request.user,
                        'ACCEPT_GALLERY',
                        reason=entry_reason,
                        content_object=gallery,
                        result='accepted'
                    )

                elif gallery_entry.submit_url:
                    message = 'Queueing URL: {}'.format(
                        gallery_entry.submit_url
                    )
                    if 'reason' in p and p['reason'] != '':
                        message += ', reason: {}'.format(p['reason'])
                    logger.info("User {}: {}".format(request.user.username, message))
                    messages.success(request, message)

                    gallery_entry.mark_as_approved(reason=entry_reason, comment=entry_comment)

                    current_settings = Settings(load_from_config=crawler_settings.config)

                    if current_settings.workers.web_queue:

                        current_settings.replace_metadata = True
                        current_settings.retry_failed = True

                        if 'reason' in p and p['reason'] != '':
                            reason = p['reason']
                            # Force limit string length (reason field max_length)
                            current_settings.archive_reason = reason[:200]
                            current_settings.gallery_reason = reason[:200]

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
                            (gallery_entry.submit_url,),
                            override_options=current_settings,
                            archive_callback=archive_callback,
                            gallery_callback=gallery_callback,
                        )

                        event_log(
                            request.user,
                            'ACCEPT_URL',
                            reason=entry_reason,
                            content_object=gallery_entry,
                            result='accepted'
                        )

    providers = Gallery.objects.all().values_list('provider', flat=True).distinct()

    submit_entries: QuerySet[GallerySubmitEntry] = GallerySubmitEntry.objects.all().prefetch_related(
        'gallery', 'similar_galleries'
    ).order_by('-submit_date')

    if 'filter_galleries' in get:
        params: dict[str, str] = {
        }

        for k, v in get.items():
            if isinstance(v, str):
                params[k] = v

        for k in gallery_filter_keys:
            if k not in params:
                params[k] = ''

        gallery_results = filter_galleries_simple(params)

        submit_entries = submit_entries.filter(gallery__in=gallery_results)

    allowed_resolved_status = [GallerySubmitEntry.RESOLVED_SUBMITTED]

    if 'denied' in get:
        allowed_resolved_status.append(GallerySubmitEntry.RESOLVED_DENIED)
    if 'approved' in get:
        allowed_resolved_status.append(GallerySubmitEntry.RESOLVED_APPROVED)
    if 'already_present' in get:
        allowed_resolved_status.append(GallerySubmitEntry.RESOLVED_ALREADY_PRESENT)

    submit_entries = submit_entries.filter(resolved_status__in=allowed_resolved_status)

    if 'submit_reason' in get:
        submit_entries = submit_entries.filter(submit_reason__icontains=get['submit_reason'])

    if 'has_similar' in get:
        submit_entries = submit_entries.annotate(similar_count=Count('similar_galleries')).filter(similar_count__gt=0)

    submit_entries = submit_entries.annotate(archive_count=Count('gallery__archive'))

    if 'has_archives' in get:
        submit_entries = submit_entries.filter(archive_count__gt=0)

    if 'gallery-public' in get:
        submit_entries = submit_entries.filter(gallery__public=True)

    submit_entries = submit_entries.annotate(archives_recycled=Count('gallery__archive', filter=Q(gallery__archive__binned=True)))

    paginator = Paginator(submit_entries, 50)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {'results': results_page, 'providers': providers, 'form': form}
    return render(request, "viewer/collaborators/submit_queue.html", d)


def filter_by_marks(archives: QuerySet[Archive], params: QueryDict) -> tuple[QuerySet[Archive], bool]:
    mark_filters = False

    if 'marked' in params and params["marked"]:
        archives = archives.filter(manage_entries__mark_check=True)
        mark_filters = True
    if 'mark_reason' in params and params["mark_reason"]:
        archives = archives.filter(manage_entries__mark_reason__contains=params["mark_reason"])
        mark_filters = True
    if 'mark_comment' in params and params["mark_comment"]:
        archives = archives.filter(manage_entries__mark_comment__contains=params["mark_comment"])
        mark_filters = True
    if 'mark_extra' in params and params["mark_extra"]:
        archives = archives.filter(manage_entries__mark_extra__contains=params["mark_extra"])
        mark_filters = True
    if 'origin' in params and params["origin"]:
        archives = archives.filter(manage_entries__origin=params["origin"])
        mark_filters = True
    priority_to = params.get("priority_to", None)
    if priority_to is not None and priority_to != '':
        archives = archives.filter(manage_entries__mark_priority__lte=float(priority_to))
        mark_filters = True
    priority_from = params.get("priority_from", None)
    if priority_from is not None and priority_from != '':
        archives = archives.filter(manage_entries__mark_priority__gte=float(priority_from))
        mark_filters = True
    elif mark_filters:
        # By default, don't filter marks with less than 1 priority (low level priorities)
        archives = archives.annotate(
            num_manage_total=Count('manage_entries'),
            num_manage_below_1=Count('manage_entries', filter=Q(manage_entries__mark_priority__lt=1))
        ).exclude(num_manage_below_1=F('num_manage_total'))

    archives = archives.distinct()

    return archives, mark_filters


@permission_required('viewer.manage_archive')
def manage_archives(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", '')
    tags = get.get("tags", '')

    user_reason = p.get('reason', '')
    json_request = get.get('json', '')

    try:
        page = int(get.get("page", '1'))
    except ValueError:
        page = 1

    try:
        size = int(get.get("size", '100'))
        if size not in (10, 20, 50, 100):
            size = 100
    except ValueError:
        size = 100

    if p:
        pks = []
        for k, v in p.items():
            if k.startswith("sel-"):
                # k, pk = k.split('-')
                # results[pk][k] = v
                pks.append(v)

        if 'run_for_all' in p and request.user.is_staff:
            params = {
                'sort': 'create_date',
                'asc_desc': 'desc',
            }

            for k, v in get.items():
                if isinstance(v, str):
                    params[k] = v

            for k in archive_filter_keys:
                if k not in params:
                    params[k] = ''

            if 'sort_by' in get:
                params['sort_by'] = get.get('sort_by', '')

            archives = filter_archives_simple(params, request.user.is_authenticated, show_binned=True)

            archives, _ = filter_by_marks(archives, request.GET)

            if 'recycled' in get and request.user.has_perm('viewer.recycle_archive'):
                archives = archives.filter(binned=True)
            else:
                archives = archives.filter(binned=False)

            if 'downloading' in get:
                archives = archives.filter(crc32='')
        else:
            preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])
            archives = Archive.objects.filter(id__in=pks).order_by(preserved)
        if 'publish_archives' in p and request.user.has_perm('viewer.publish_archive'):
            for archive in archives:
                message = 'Publishing archive: {}, link: {}'.format(
                    archive.title, archive.get_absolute_url()
                )
                if 'reason' in p and p['reason'] != '':
                    message += ', reason: {}'.format(p['reason'])
                logger.info("User {}: {}".format(request.user.username, message))
                if not json_request:
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
                logger.info("User {}: {}".format(request.user.username, message))
                if not json_request:
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
                message = 'Deleting archive: {}, link: {}, with it\'s file: {}'.format(
                    archive.title, archive.get_absolute_url(),
                    archive.zipped.path
                )
                gallery = archive.gallery
                archive_report = archive.delete_text_report()
                if archive.gallery:
                    if 'mark_delete_galleries' in p and p['mark_delete_galleries'] and request.user.has_perm('viewer.mark_delete_gallery'):
                        archive.gallery.mark_as_deleted()
                        message += ', gallery: {} will be marked as deleted'.format(archive.gallery.get_absolute_url())
                        archive.gallery = None
                        event_log(
                            request.user,
                            'MARK_DELETE_GALLERY',
                            data=archive_report,
                            reason=user_reason,
                            content_object=gallery,
                            result='success',
                        )
                    elif 'deny_galleries' in p and p['deny_galleries'] and request.user.has_perm('viewer.approve_gallery'):
                        archive.gallery.mark_as_denied()
                        message += ', gallery: {} will be marked as denied'.format(archive.gallery.get_absolute_url())
                        archive.gallery = None
                        event_log(
                            request.user,
                            'DENY_GALLERY',
                            data=archive_report,
                            reason=user_reason,
                            content_object=gallery,
                            result='success',
                        )
                    elif 'delete_galleries' in p and p['delete_galleries'] and request.user.has_perm('viewer.delete_gallery'):
                        old_gallery_link = archive.gallery.get_link()
                        archive.gallery.delete()
                        archive.gallery = None
                        event_log(
                            request.user,
                            'DELETE_GALLERY',
                            reason=user_reason,
                            data=old_gallery_link,
                            result='success',
                        )
                        message += ', gallery: {} will be deleted'.format(old_gallery_link)
                    else:
                        message += ', gallery: {} will be kept'.format(archive.gallery.get_absolute_url())
                archive.delete_all_files()
                archive.delete()
                event_log(
                    request.user,
                    'DELETE_ARCHIVE',
                    content_object=gallery,
                    reason=user_reason,
                    result='deleted',
                    data=archive_report
                )
                if 'reason' in p and p['reason'] != '':
                    message += ', reason: {}'.format(p['reason'])
                logger.info("User {}: {}".format(request.user.username, message))
                if not json_request:
                    messages.success(request, message)
        elif 'update_metadata' in p and request.user.has_perm('viewer.update_metadata'):

            galleries_from_archives = Gallery.objects.filter(archive__in=archives).distinct()

            gallery_providers = list(galleries_from_archives.values_list('provider', flat=True).distinct())

            providers_filtered = [x for x in gallery_providers if x is not None]

            message = 'Updating gallery API data for {} galleries and related archives'.format(
                galleries_from_archives.count()
            )
            if 'reason' in p and p['reason'] != '':
                message += ', reason: {}'.format(p['reason'])
            logger.info("User {}: {}".format(request.user.username, message))
            if not json_request:
                messages.success(request, message)

            current_settings = Settings(load_from_config=crawler_settings.config)

            if current_settings.workers.web_queue:
                current_settings.set_update_metadata_options(providers=providers_filtered)

                def gallery_callback(x: Optional['Gallery'], crawled_url: Optional[str], result: str) -> None:
                    event_log(
                        request.user,
                        'UPDATE_METADATA',
                        reason=user_reason,
                        content_object=x,
                        result=result,
                        data=crawled_url
                    )

                gallery_links = [x.get_link() for x in galleries_from_archives]

                current_settings.workers.web_queue.enqueue_args_list(
                    gallery_links,
                    override_options=current_settings,
                    gallery_callback=gallery_callback
                )

        elif 'recalc_fileinfo' in p and request.user.has_perm('viewer.recalc_fileinfo'):
            for archive in archives:
                message = 'Recalculating file information for archive: {}, link: {}'.format(
                    archive.title, archive.get_absolute_url()
                )
                if 'reason' in p and p['reason'] != '':
                    message += ', reason: {}'.format(p['reason'])
                logger.info("User {}: {}".format(request.user.username, message))
                if not json_request:
                    messages.success(request, message)
                archive.recalc_fileinfo()
                event_log(
                    request.user,
                    'RECALC_ARCHIVE',
                    reason=user_reason,
                    content_object=archive,
                    result='success'
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
                            logger.info("User {}: {}".format(request.user.username, message))
                            if not json_request:
                                messages.success(request, message)
                            event_log(
                                request.user,
                                'ADD_ARCHIVE_TO_GROUP',
                                content_object=archive,
                                reason=user_reason,
                                result='added'
                            )
        elif 'recycle_archives' in p and request.user.is_authenticated and request.user.has_perm('viewer.recycle_archive'):
            for archive in archives:
                if not archive.is_recycled():
                    message = 'Moving to Recycle Bin archive: {}, link: {}'.format(
                        archive.title, archive.get_absolute_url()
                    )
                    if 'reason' in p and p['reason'] != '':
                        message += ', reason: {}'.format(p['reason'])
                    logger.info("User {}: {}".format(request.user.username, message))
                    if not json_request:
                        messages.success(request, message)
                    r = ArchiveRecycleEntry(
                        archive=archive,
                        reason=user_reason,
                        user=request.user,
                        origin=ArchiveRecycleEntry.ORIGIN_USER,
                    )

                    r.save()
                    archive.binned = True
                    archive.simple_save()
                    event_log(
                        request.user,
                        'MOVE_TO_RECYCLE_BIN',
                        reason=user_reason,
                        content_object=archive,
                        result='recycled'
                    )
        elif 'reduce_archives' in p and request.user.is_authenticated and request.user.has_perm('viewer.expand_archive'):
            for archive in archives:
                if archive.extracted:
                    message = 'Reducing Archive: {}, link: {}'.format(
                        archive.title, archive.get_absolute_url()
                    )
                    if 'reason' in p and p['reason'] != '':
                        message += ', reason: {}'.format(p['reason'])
                    logger.info("User {}: {}".format(request.user.username, message))
                    if not json_request:
                        messages.success(request, message)
                    
                    archive.reduce()
                    event_log(
                        request.user,
                        'REDUCE_ARCHIVE',
                        reason=user_reason,
                        content_object=archive,
                        result='success'
                    )
        elif 'mark_similar' in p and request.user.is_authenticated and request.user.has_perm('viewer.mark_similar_archive'):
            for archive in archives:
                message = 'Creating similar info as marks for Archive: {}'.format(archive.get_absolute_url())
                if 'reason' in p and p['reason'] != '':
                    message += ', reason: {}'.format(p['reason'])
                logger.info("User {}: {}".format(request.user.username, message))
                if not json_request:
                    messages.success(request, message)

                archive.create_marks_for_similar_archives()

        if json_request:
            return HttpResponse('', content_type="application/json; charset=utf-8")

    params = {
        'sort': 'create_date',
        'asc_desc': 'desc',
    }

    for k, v in get.items():
        if isinstance(v, str):
            params[k] = v

    for k in archive_filter_keys:
        if k not in params:
            params[k] = ''

    if 'sort_by' in get:
        params['sort_by'] = get.get('sort_by', '')

    results = filter_archives_simple(params, request.user.is_authenticated, show_binned=True)

    results, mark_filters = filter_by_marks(results, request.GET)

    if 'recycled' in get and request.user.has_perm('viewer.recycle_archive'):
        results = results.filter(binned=True)
    else:
        results = results.filter(binned=False)

    if 'downloading' in get:
        results = results.filter(crc32='')

    if 'no-custom-tags' in get:
        results = results.annotate(num_custom_tags=Count('tags', filter=Q(archivetag__origin=ArchiveTag.ORIGIN_USER))).filter(num_custom_tags=0)

    if 'with-custom-tags' in get:
        results = results.annotate(num_custom_tags=Count('tags', filter=Q(archivetag__origin=ArchiveTag.ORIGIN_USER))).filter(num_custom_tags__gt=0)

    results = results.select_related('gallery', 'user')

    if json_request:
        results = results.prefetch_related('tags')

    if request.user.has_perm('viewer.view_marks'):
        results = results.prefetch_related('manage_entries')

    paginator = Paginator(results, size)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    if json_request:
        response = json.dumps(
            {
                'results': archive_manage_results_to_json(request, results_page, request.user.is_authenticated),
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

    if 'clear' in get:
        form = ArchiveSearchForm()
    else:
        form = ArchiveSearchForm(initial={'title': title, 'tags': tags})

    mark_form_simple = ArchiveManageEntrySimpleForm(request.GET)
    search_form = ArchiveManageSearchSimpleForm(request.GET)

    d = {
        'results': results_page,
        'form': form,
        'mark_filters': mark_filters,
        'mark_form_simple': mark_form_simple,
        'search_form': search_form,
    }

    if request.user.has_perm('viewer.change_archivegroup'):
        group_form = ArchiveGroupSelectForm()
        d.update(group_form=group_form)

    return render(request, "viewer/collaborators/manage_archives.html", d)


@login_required
def my_event_log(request: AuthenticatedHttpRequest) -> HttpResponse:
    get = request.GET

    try:
        page = int(get.get("page", '1'))
    except ValueError:
        page = 1

    selected_actions = get.getlist("actions")

    if selected_actions:
        results = EventLog.objects.filter(action__in=selected_actions)
    else:
        results = EventLog.objects.all()

    results = results.filter(user=request.user).prefetch_related('content_object')

    actions = EventLog.objects.filter(user=request.user).order_by().values_list('action', flat=True).distinct()

    paginator = Paginator(results, 100)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {
        'results': results_page,
        'actions': actions
    }
    return render(request, "viewer/collaborators/event_log.html", d)


@permission_required('viewer.read_activity_logs')
def activity_event_log(request: HttpRequest) -> HttpResponse:
    get = request.GET

    try:
        page = int(get.get("page", '1'))
    except ValueError:
        page = 1

    selected_actions = get.getlist("actions")

    data_field = get.get("data_field", '')
    title = get.get("title", '')
    tags = get.get("tags", '')
    filter_galleries = get.get("filter-galleries", '')
    filter_archives = get.get("filter-archives", '')
    event_date_from = get.get("event_date_from", '')
    event_date_to = get.get("event_date_to", '')
    if request.user.is_staff or request.user.has_perm('viewer.read_all_logs'):
        show_users = get.get("show-users", '')
    else:
        show_users = ''

    if 'clear' in get:
        form = AllGalleriesSearchForm()
        form_event = EventSearchForm()
    else:
        form = AllGalleriesSearchForm(initial={'title': title, 'tags': tags})
        form_event = EventSearchForm(
            initial={'data_field': data_field, 'event_date_from': event_date_from, 'event_date_to': event_date_to}
        )

    if selected_actions:
        results = EventLog.objects.filter(action__in=selected_actions).prefetch_related('content_object', 'content_type')
    else:
        results = EventLog.objects.all().prefetch_related('content_object', 'content_type')

    if show_users:
        results = results.select_related('user')

    if event_date_from:
        results = results.filter(create_date__gte=event_date_from)
    if event_date_to:
        results = results.filter(create_date__lte=event_date_to)

    if data_field:
        results = results.filter(data__contains=data_field)
        
    event_content_type = get.get("content-type", '')
    event_content_id = get.get("content-id", '')
    
    if event_content_type in ('archive', 'gallery'):
        if event_content_type == 'archive':
            event_type = ContentType.objects.get_for_model(Archive)
            results = results.filter(content_type=event_type)
        elif event_content_type == 'gallery':
            event_type = ContentType.objects.get_for_model(Gallery)
            results = results.filter(content_type=event_type)
            
    if event_content_id:
        results = results.filter(object_id=event_content_id)

    if title or tags and (filter_galleries or filter_archives):

        total_filters = Q()

        if filter_galleries:
            params = {
            }

            for k, v in get.items():
                if isinstance(v, str):
                    params[k] = v

            for k in gallery_filter_keys:
                if k not in params:
                    params[k] = ''

            gallery_results = filter_galleries_simple(params)

            gallery_type = ContentType.objects.get_for_model(Gallery)
            total_filters.add(Q(object_id__in=gallery_results, content_type=gallery_type), Q.OR)

        if filter_archives:
            params = {
                'sort': 'create_date',
                'asc_desc': 'desc',
            }

            for k, v in get.items():
                if isinstance(v, str):
                    params[k] = v

            for k in archive_filter_keys:
                if k not in params:
                    params[k] = ''

            archive_results = filter_archives_simple(params, request.user.is_authenticated, show_binned=True)

            archive_type = ContentType.objects.get_for_model(Archive)
            total_filters.add(Q(object_id__in=archive_results, content_type=archive_type), Q.OR)

        results = results.filter(total_filters)

    actions = EventLog.objects.order_by('action').values_list('action', flat=True).distinct()

    paginator = Paginator(results, 100)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {
        'results': results_page,
        'actions': actions,
        'form': form,
        'form_event': form_event,
        'show_users': show_users
    }
    return render(request, "viewer/collaborators/activity_event_log.html", d)


@permission_required('viewer.crawler_adder')
def user_crawler(request: AuthenticatedHttpRequest) -> HttpResponse:
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
        # Allow collaborators to readd a gallery if it failed.
        current_settings.retry_failed = True
        current_settings.config['allowed']['retry_failed'] = 'yes'
        for k, v in p.items():
            if isinstance(v, str):
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

        add_as_deleted = 'as-deleted' in p and p['as-deleted'] == '1'

        skip_non_current = 'skip-non-current' in p and p['skip-non-current'] == '1'

        if add_as_deleted and request.user.has_perm('viewer.add_deleted_gallery'):
            # Use this to download using info
            current_settings.allow_type_downloaders_only('info')

            def gallery_callback(x: Optional['Gallery'], crawled_url: Optional[str], result: str) -> None:
                event_log(
                    request.user,
                    'ADD_DELETED_GALLERY',
                    reason=user_reason,
                    content_object=x,
                    result=result,
                    data=crawled_url
                )
                if x:
                    x.mark_as_deleted()
        else:
            def gallery_callback(x: Optional['Gallery'], crawled_url: Optional[str], result: str) -> None:
                event_log(
                    request.user,
                    'ADD_GALLERY',
                    reason=user_reason,
                    content_object=x,
                    result=result,
                    data=crawled_url
                )

        current_settings.archive_user = request.user
        current_settings.archive_origin = Archive.ORIGIN_ADD_URL

        if skip_non_current:
            current_settings.non_current_links_as_deleted = True

        parsers = crawler_settings.provider_context.get_parsers(crawler_settings)

        def archive_callback(x: Optional['Archive'], crawled_url: Optional[str], result: str) -> None:
            event_log(
                request.user,
                'ADD_ARCHIVE',
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
                    if not gallery:
                        if add_as_deleted:
                            messages.warning(
                                request,
                                '{}: New URL, will be added as deleted'.format(url_filtered)
                            )
                        else:
                            messages.success(
                                request,
                                '{}: New URL, will be processed'.format(url_filtered)
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
                    elif gallery.is_deleted():
                        messages.warning(
                            request,
                            '{}: Already present, marked as deleted: {}, reason: {}'.format(
                                url_filtered, gallery.get_absolute_url(), gallery.reason
                            )
                        )
                        event_log(
                            request.user,
                            'CRAWL_URL',
                            reason=user_reason,
                            data=url_filtered,
                            result='already_deleted'
                        )
                    elif gallery.is_denied():
                        messages.warning(
                            request,
                            '{}: Already present, marked as denied: {}, reason: {}'.format(
                                url_filtered, gallery.get_absolute_url(), gallery.reason
                            )
                        )
                        event_log(
                            request.user,
                            'CRAWL_URL',
                            reason=user_reason,
                            data=url_filtered,
                            result='already_denied'
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

    try:
        limit = max(1, int(get.get("limit", '100')))
    except ValueError:
        limit = 100

    try:
        inline_thumbnails = bool(get.get("inline-thumbnails", ''))
    except ValueError:
        inline_thumbnails = False

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
                cutoff = clamp(float(p.get('cutoff', '0.4')), 0.0, 1.0)
            except ValueError:
                cutoff = 0.4
            try:
                max_matches = int(p.get('max-matches', '10'))
            except ValueError:
                max_matches = 10

            logger.info(
                'User {}: Looking for possible matches in gallery database '
                'for non-matched archives (cutoff: {}, max matches: {}) '
                'using provider filter "{}"'.format(request.user.username, cutoff, max_matches, provider)
            )
            matching_thread = threading.Thread(
                name='match_unmatched_worker',
                target=generate_possible_matches_for_archives,
                args=(archives,),
                kwargs={
                    'cutoff': cutoff, 'max_matches': max_matches, 'filters': (provider,),
                    'match_local': True, 'match_web': False
                })
            matching_thread.daemon = True
            matching_thread.start()
            messages.success(request, 'Starting internal match worker.')
        elif 'clear_possible_matches' in p:

            for archive in archives:
                archive.possible_matches.clear()

            logger.info(
                'User {}: Clearing possible matches for archives'.format(request.user.username)
            )
            messages.success(request, 'Clearing possible matches.')

    params: dict[str, str] = {
        'sort': 'create_date',
        'asc_desc': 'desc',
    }

    for k, v in get.items():
        if isinstance(v, str):
            params[k] = v

    for k in archive_filter_keys:
        if k not in params:
            params[k] = ''

    results = filter_archives_simple(params, authenticated=request.user.is_authenticated)

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
                ),
                Prefetch(
                    'gallery__tags',
                    queryset=Tag.objects.filter(scope__exact='magazine'),
                    to_attr='magazine_tags'
                )
            ),
            to_attr='possible_galleries'
        ),
        'possible_galleries__gallery',
    )

    if 'with-possible-matches' in get:
        results = results.annotate(n_possible_matches=Count('possible_matches')).filter(n_possible_matches__gt=0)

    paginator = Paginator(results, limit)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {
        'results': results_page,
        'providers': Gallery.objects.all().values_list('provider', flat=True).distinct(),
        'form': form,
        'inline_thumbnails': inline_thumbnails
    }
    return render(request, "viewer/collaborators/unmatched_archives.html", d)


@login_required
def archive_update(request: HttpRequest, pk: int, tool: Optional[str] = None, tool_use_id: Optional[str] = None) -> HttpResponse:
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if tool == 'select-as-match' and tool_use_id and request.user.has_perm('viewer.match_archive'):
        try:
            gallery_id = int(tool_use_id)
            archive.select_as_match(gallery_id)
            if archive.gallery:
                logger.info("User: {}: Archive {} ({}) was matched with gallery {} ({}).".format(
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
        except ValueError:
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == 'clear-possible-matches' and request.user.has_perm('viewer.match_archive'):
        archive.possible_matches.clear()
        logger.info("User: {}: Archive {} ({}) was cleared from its possible matches.".format(
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
        form = WantedGalleryColSearchForm()
    else:
        form = WantedGalleryColSearchForm(initial={'title': title, 'tags': tags})

    if request.POST.get('submit-wanted-gallery') and request.user.has_perm('viewer.add_wantedgallery'):
        # create a form instance and populate it with data from the request:
        edit_form = WantedGalleryCreateOrEditForm(request.POST)
        # check whether it's valid:
        if edit_form.is_valid():
            new_wanted_gallery = edit_form.save()
            message = 'New wanted gallery successfully created'
            messages.success(request, message)
            logger.info("User {}: {}".format(request.user.username, message))
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
    )

    paginator = Paginator(results, 100)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {'results': results_page, 'form': form, 'edit_form': edit_form}
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
            logger.info("User {}: {}".format(request.user.username, message))
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
            new_archive.origin = Archive.ORIGIN_UPLOAD_ARCHIVE
            new_archive = edit_form.save()
            if crawler_settings.mark_similar_new_archives:
                new_archive.create_marks_for_similar_archives()
            message = 'Archive successfully uploaded: {}'.format(new_archive.get_absolute_url())
            messages.success(request, message)
            logger.info("User {}: {}".format(request.user.username, message))
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

        return render(request, "viewer/include/messages.html")

    else:
        if 'gallery' in request.GET:
            try:
                gallery_id = int(request.GET['gallery'])
                try:
                    gallery: Optional[Gallery] = Gallery.objects.get(pk=gallery_id)
                except Gallery.DoesNotExist:
                    gallery = None
            except ValueError:
                gallery = None
        else:
            gallery = None

        if gallery:
            edit_form = ArchiveCreateForm(
                initial={'gallery': gallery, 'reason': gallery.reason, 'source_type': gallery.provider}
            )
        else:
            edit_form = ArchiveCreateForm()

    d = {
        'edit_form': edit_form
    }
    return render(request, "viewer/collaborators/add_archive.html", d)


# This is an alternative way to add Galleries, the preffered way is to crawl a link.
# Mostly used in scenarios when the metadata is already fetched on another instance or backup.
# Problem is that it only accepts already created tags, magazine, gallery_container
@permission_required('viewer.add_gallery')
def upload_gallery(request: HttpRequest) -> HttpResponse:
    if request.POST.get('submit-gallery'):
        # create a form instance and populate it with data from the request:
        edit_form = GalleryCreateForm(request.POST, request.FILES)
        # check whether it's valid:
        if edit_form.is_valid():
            new_gallery = edit_form.save(commit=False)
            new_gallery.save()
            edit_form.save_m2m()
            message = 'Gallery successfully created: {}'.format(new_gallery.get_absolute_url())
            messages.success(request, message)
            logger.info("User {}: {}".format(request.user.username, message))
            event_log(
                request.user,
                'CREATE_GALLERY',
                content_object=new_gallery,
                result='added'
            )
        else:
            messages.error(request, 'The provided data is not valid', extra_tags='danger')
    else:
        if 'gallery' in request.GET:
            try:
                gallery_id = int(request.GET['gallery'])
                try:
                    gallery: Optional[Gallery] = Gallery.objects.get(pk=gallery_id)
                except Gallery.DoesNotExist:
                    gallery = None
            except ValueError:
                gallery = None
        else:
            gallery = None

        if gallery:
            edit_form = GalleryCreateForm(
                instance=gallery
            )
        else:
            edit_form = GalleryCreateForm()

    d = {
        'edit_form': edit_form
    }
    return render(request, "viewer/collaborators/add_gallery.html", d)


@permission_required('viewer.add_wantedgallery')
def create_wanted_gallery(request: HttpRequest) -> HttpResponse:
    if request.POST.get('submit-wanted-gallery'):
        # create a form instance and populate it with data from the request:
        edit_form = WantedGalleryCreateOrEditForm(request.POST)
        # check whether it's valid:
        if edit_form.is_valid():
            new_wanted_gallery = edit_form.save(commit=False)
            new_wanted_gallery.save()
            edit_form.save_m2m()
            message = 'WantedGallery successfully created: {}'.format(new_wanted_gallery.get_absolute_url())
            messages.success(request, message)
            logger.info("User {}: {}".format(request.user.username, message))
            event_log(
                request.user,
                'CREATE_WANTED_GALLERY',
                content_object=new_wanted_gallery,
                result='added'
            )
        else:
            messages.error(request, 'The provided data is not valid', extra_tags='danger')
    else:
        if 'wanted-gallery' in request.GET:
            try:
                wanted_gallery_id = int(request.GET['wanted-gallery'])
                try:
                    wg_instance: Optional[WantedGallery] = WantedGallery.objects.get(pk=wanted_gallery_id)
                except Gallery.DoesNotExist:
                    wg_instance = None
            except ValueError:
                wg_instance = None
        else:
            wg_instance = None

        if wg_instance:
            edit_form = WantedGalleryCreateOrEditForm(
                instance=wg_instance
            )
        else:
            edit_form = WantedGalleryCreateOrEditForm()

    d = {
        'edit_form': edit_form
    }
    return render(request, "viewer/collaborators/add_wanted_gallery.html", d)


@permission_required('viewer.manage_missing_archives')
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

    if p:
        pks = []
        for k, v in p.items():
            if k.startswith("sel-"):
                # k, pk = k.split('-')
                # results[pk][k] = v
                pks.append(v)

        if 'reason' in p and p['reason'] != '':
            reason = p['reason']
        else:
            reason = ''

        preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])

        results_gallery = Gallery.objects.filter(id__in=pks).order_by(preserved)

        if 'delete_galleries' in p and request.user.has_perm('viewer.mark_delete_gallery'):
            for gallery in results_gallery:
                message = 'Marking deleted for gallery: {}, link: {}'.format(gallery.title, gallery.get_link())
                logger.info(message)
                messages.success(request, message)
                gallery.mark_as_deleted()
                event_log(
                    request.user,
                    'MARK_DELETE_GALLERY',
                    reason=reason,
                    content_object=gallery,
                    result='success',
                )
        elif 'real_delete_galleries' in p and request.user.has_perm('viewer.delete_gallery'):
            for gallery in results_gallery:
                message = 'Deleting gallery: {}, link: {}'.format(gallery.title, gallery.get_link())
                old_gallery_link = gallery.get_link()
                logger.info(message)
                messages.success(request, message)
                gallery.delete()
                event_log(
                    request.user,
                    'DELETE_GALLERY',
                    reason=reason,
                    data=old_gallery_link,
                    result='success',
                )
        elif 'publish_galleries' in p and request.user.has_perm('viewer.publish_gallery'):
            for gallery in results_gallery:
                message = 'Publishing gallery: {}, link: {}'.format(gallery.title, gallery.get_link())
                logger.info(message)
                messages.success(request, message)
                gallery.set_public()
                event_log(
                    request.user,
                    'PUBLISH_GALLERY',
                    reason=reason,
                    content_object=gallery,
                    result='success',
                )
        elif 'private_galleries' in p and request.user.has_perm('viewer.private_gallery'):
            for gallery in results_gallery:
                message = 'Making private gallery: {}, link: {}'.format(gallery.title, gallery.get_link())
                logger.info(message)
                messages.success(request, message)
                gallery.set_private()
                event_log(
                    request.user,
                    'UNPUBLISH_GALLERY',
                    reason=reason,
                    content_object=gallery,
                    result='success',
                )
        elif 'download_galleries' in p and request.user.has_perm('viewer.download_gallery'):
            for gallery in results_gallery:
                message = 'Queueing gallery: {}, link: {}'.format(gallery.title, gallery.get_link())
                logger.info(message)
                messages.success(request, message)

                # Force replace_metadata when queueing from this list, since it's mostly used to download non used.
                current_settings = Settings(load_from_config=crawler_settings.config)

                if current_settings.workers.web_queue:

                    current_settings.replace_metadata = True
                    current_settings.retry_failed = True

                    if reason:
                        # Force limit string length (reason field max_length)
                        current_settings.archive_reason = reason[:200]
                        current_settings.archive_details = gallery.reason or ''
                        current_settings.gallery_reason = reason[:200]
                    elif gallery.reason:
                        current_settings.archive_reason = gallery.reason

                    def archive_callback(x: Optional['Archive'], crawled_url: Optional[str], result: str) -> None:
                        event_log(
                            request.user,
                            'DOWNLOAD_ARCHIVE',
                            reason=reason,
                            content_object=x,
                            result=result,
                            data=crawled_url
                        )

                    def gallery_callback(x: Optional['Gallery'], crawled_url: Optional[str], result: str) -> None:
                        event_log(
                            request.user,
                            'DOWNLOAD_GALLERY',
                            reason=reason,
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
        elif 'recall_api' in p and request.user.has_perm('viewer.update_metadata'):
            message = 'Recalling API for {} galleries'.format(results_gallery.count())
            logger.info(message)
            messages.success(request, message)

            gallery_links = [x.get_link() for x in results_gallery]
            gallery_providers = list(results_gallery.values_list('provider', flat=True).distinct())

            galleries_update_metadata(gallery_links, gallery_providers, request.user, reason, crawler_settings)

    providers = Gallery.objects.all().values_list('provider', flat=True).distinct()

    params = {
    }

    for k, v in get.items():
        if isinstance(v, str):
            params[k] = v

    for k in gallery_filter_keys:
        if k not in params:
            params[k] = ''

    results = filter_galleries_simple(params)

    results = results.non_used_galleries().prefetch_related('foundgallery_set')  # type: ignore

    paginator = Paginator(results, 50)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {'results': results_page, 'providers': providers, 'form': form}

    return render(request, "viewer/collaborators/archives_missing_for_galleries.html", d)


@permission_required('viewer.manage_archive')
def archives_similar_by_fields(request: HttpRequest) -> HttpResponse:
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
        groups = defaultdict(list)
        group_mains = defaultdict(list)
        for k, v in p.items():
            if isinstance(v, str):
                if k.startswith("del-"):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    _, pk = k.split('-')
                    pks.append(int(pk))
                    groups[int(pk)].append(int(v))
                if k.startswith("main-"):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    _, pk = k.split('-')
                    group_mains[int(v)].append(int(pk))

        archives = Archive.objects.filter(id__in=pks).order_by('-create_date')

        user_reason = p.get('reason', '')

        if 'delete_archives' in p and request.user.has_perm('viewer.delete_archive'):
            for archive in archives:
                message = 'Removing archive: {} and deleting file: {}'.format(
                    archive.get_absolute_url(), archive.zipped.path
                )
                logger.info(message)
                messages.success(request, message)

                gallery = archive.gallery
                archive_report = archive.delete_text_report()

                if gallery:
                    for group_id in groups[archive.pk]:
                        if group_id in group_mains:
                            for main_id in group_mains[group_id]:
                                try:
                                    main_archive = Archive.objects.get(pk=main_id)
                                except Archive.DoesNotExist:
                                    continue
                                main_archive.alternative_sources.add(gallery)
                                message = 'Adding to archive: {}, gallery as alternative source: {}'.format(
                                    main_archive.get_absolute_url(), gallery.get_absolute_url()
                                )
                                if main_archive.public and not gallery.public:
                                    gallery.set_public()
                                logger.info(message)
                                messages.success(request, message)

                if gallery and gallery.archive_set.count() <= 1 and gallery.alternative_sources.count() < 1:
                    gallery.mark_as_deleted()
                archive.delete_all_files()
                archive.delete()

                event_log(
                    request.user,
                    'DELETE_ARCHIVE',
                    reason=user_reason,
                    content_object=gallery,
                    result='deleted',
                    data=archive_report
                )

        elif 'delete_objects' in p and request.user.has_perm('viewer.delete_archive'):
            for archive in archives:
                message = 'Removing archive: {}, keeping file: {}'.format(
                    archive.get_absolute_url(), archive.zipped.path
                )
                logger.info(message)
                messages.success(request, message)

                gallery = archive.gallery
                archive_report = archive.delete_text_report()

                if gallery:
                    for group_id in groups[archive.pk]:
                        if group_id in group_mains:
                            for main_id in group_mains[group_id]:
                                try:
                                    main_archive = Archive.objects.get(pk=main_id)
                                except Archive.DoesNotExist:
                                    continue
                                main_archive.alternative_sources.add(gallery)
                                message = 'Adding to archive: {}, gallery as alternative source: {}'.format(
                                    main_archive.get_absolute_url(), gallery.get_absolute_url()
                                )
                                if main_archive.public and not gallery.public:
                                    gallery.set_public()
                                logger.info(message)
                                messages.success(request, message)

                if gallery and gallery.archive_set.count() <= 1 and gallery.alternative_sources.count() < 1:
                    gallery.mark_as_deleted()
                archive.delete_files_but_archive()
                archive.delete()

                event_log(
                    request.user,
                    'DELETE_ARCHIVE',
                    reason=user_reason,
                    content_object=gallery,
                    result='deleted',
                    data=archive_report
                )

        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    params = {
        'sort': 'create_date',
        'asc_desc': 'desc',
    }

    for k, v in get.items():
        if isinstance(v, str):
            params[k] = v

    for k in archive_filter_keys:
        if k not in params:
            params[k] = ''

    after_source_type = params['source_type']
    after_reason = params['reason']

    def source_lambda(x):
        return x.source_type == after_source_type

    def reason_lambda(x):
        return x.reason == after_reason

    if 'clear-title' in get:
        def clear_archive_title(x: Archive):
            return re.sub(r'[^A-Za-z0-9 ]+', '', re.sub(r'\s+\(.+?\)', r'', re.sub(r'\[.+?\]\s*', r'', x.best_title))).lower().strip()
    else:
        def clear_archive_title(x: Archive):
            return x.best_title

    if 'filter-after' in get:
        params['source_type'] = ''
        params['reason'] = ''

    results = filter_archives_simple(params, request.user.is_authenticated)

    if 'no-custom-tags' in get:
        results = results.annotate(num_custom_tags=Count('tags', filter=Q(archivetag__origin=ArchiveTag.ORIGIN_USER))).filter(num_custom_tags=0)

    by_size_count = {}
    by_crc32 = {}
    by_title = {}
    group_count = 1
    hard_limit_groups = 400

    if 'filter-fileinfo' in get:
        for k_filesize, v_filesize in groupby(results.exclude(filesize__isnull=True).exclude(filecount__isnull=True).order_by('filesize', 'filecount').distinct(), lambda x: (x.filesize, x.filecount)):
            objects = list(v_filesize)
            if len(objects) > 1:
                if 'filter-after' in get:
                    if after_source_type and len(list(filter(source_lambda, objects))) < 1:
                        continue
                    if after_reason and len(list(filter(reason_lambda, objects))) < 1:
                        continue

                by_size_count[group_count] = objects
                group_count += 1
                if 'limit-groups' in get and len(by_size_count.keys()) >= hard_limit_groups:
                    break

    if 'filter-crc32' in get:
        for k_crc32, v_crc32 in groupby(results.exclude(crc32__isnull=True).exclude(crc32='').order_by('crc32').distinct(), lambda x: x.crc32):
            objects = list(v_crc32)
            if len(objects) > 1:
                if 'filter-after' in get:
                    if after_source_type and len(list(filter(source_lambda, objects))) < 1:
                        continue
                    if after_reason and len(list(filter(reason_lambda, objects))) < 1:
                        continue
                by_crc32[group_count] = objects
                group_count += 1
                if 'limit-groups' in get and len(by_crc32.keys()) >= hard_limit_groups:
                    break

    if 'filter-title' in get:
        query_result = results.exclude(title__isnull=True).exclude(title='').order_by('title').distinct()
        for k_title, v_title in groupby(sorted(query_result, key=clear_archive_title), clear_archive_title):
            objects = list(v_title)
            if len(objects) > 1:
                if 'filter-after' in get:
                    if after_source_type and len(list(filter(source_lambda, objects))) < 1:
                        continue
                    if after_reason and len(list(filter(reason_lambda, objects))) < 1:
                        continue
                by_title[group_count] = objects
                group_count += 1
                if 'limit-groups' in get and len(by_title.keys()) >= hard_limit_groups:
                    break

    d = {
        'by_size_count': by_size_count,
        'by_crc32': by_crc32,
        'by_title': by_title,
        'form': form
    }
    return render(request, "viewer/archives_similar_by_fields.html", d)


@permission_required('viewer.view_monitored_links')
def monitored_links(request: HttpRequest) -> HttpResponse:
    get = request.GET

    try:
        page = int(get.get("page", '1'))
    except ValueError:
        page = 1

    results = MonitoredLink.objects.all()

    paginator = Paginator(results, 100)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    schedulers_list = get_schedulers_status(crawler_settings.workers.timed_link_monitors)

    schedulers = {int(x[0].replace("link_monitor_", "")): x for x in schedulers_list}

    d = {
        'results': results_page,
        'title': 'Monitored Links',
        "schedulers": schedulers,
    }
    return render(request, "viewer/collaborators/monitored_links.html", d)


@login_required
def user_token(request: AuthenticatedHttpRequest, operation: str) -> HttpResponse:

    if operation == 'create':

        token_name = request.GET.get("token-name", None)

        if not token_name:
            messages.error(request, "Missing parameter 'token-name'.")
            return HttpResponseRedirect(request.META["HTTP_REFERER"])

        token_exists = request.user.long_lived_tokens.filter(name=token_name)
        if len(token_exists) > 0:
            messages.error(request, "Cannot create token with name: {}. Already exists.".format(token_name))
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
        else:
            secret_key = get_random_string(UserLongLivedToken.TOKEN_LENGTH, UserLongLivedToken.VALID_CHARS)

            salted_key = UserLongLivedToken.create_salted_key_from_key(secret_key)

            UserLongLivedToken.objects.create(
                key=salted_key,
                user=request.user,
                name=token_name,
            )
            messages.success(request, "Token with name: {} created successfully. Write down the token itself it will be only shown here (everything inside <>): <{}>.".format(token_name, secret_key))
    elif operation == 'delete':

        token_id = request.GET.get("id", None)

        if token_id is None:
            messages.error(request, "Missing parameter 'id'.")
            return HttpResponseRedirect(request.META["HTTP_REFERER"])

        token_exists = request.user.long_lived_tokens.filter(pk=token_id)
        if len(token_exists) < 1:
            messages.error(request, "Cannot delete token with ID: {}. It does not exist or does not belong to you.".format(token_id))
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
        else:
            token_exists.delete()
            messages.success(request, "Token with ID: {} deleted successfully.".format(token_id))

    return HttpResponseRedirect(request.META["HTTP_REFERER"])
