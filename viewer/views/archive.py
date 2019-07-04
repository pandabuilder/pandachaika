# Here are the views for interacting with the archive Model
# exclusively.

import logging
from os.path import basename
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.urls import reverse
from django.db import transaction
from django.http import Http404, HttpRequest
from django.http import HttpResponseRedirect, HttpResponse
from django.shortcuts import render
from django.conf import settings

from core.base.setup import Settings
from core.local.foldercrawlerthread import FolderCrawlerThread
from viewer.utils.actions import event_log
from viewer.forms import (
    ArchiveModForm, ImageFormSet,
    ArchiveEditForm)
from viewer.models import (
    Archive, Tag, Gallery,
    UserArchivePrefs
)
from viewer.views.head import render_error

crawler_logger = logging.getLogger('viewer.webcrawler')
folder_logger = logging.getLogger('viewer.foldercrawler')
frontend_logger = logging.getLogger('viewer.frontend')
crawler_settings = settings.CRAWLER_SETTINGS


def archive_details(request: HttpRequest, pk: int, view: str = "cover") -> HttpResponse:
    """Archive listing."""

    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")
    if not archive.public and not request.user.is_authenticated:
        raise Http404("Archive does not exist")

    if not request.user.is_authenticated:
        view = "cover"

    num_images = 30
    if view in ("full", "edit"):
        num_images = 10
    if view in ("single", "cover"):
        num_images = 1

    images = archive.image_set.filter(extracted=True)

    if images:
        paginator = Paginator(images, num_images)
        try:
            page = int(request.GET.get("page", '1'))
        except ValueError:
            page = 1

        try:
            images = paginator.page(page)
        except (InvalidPage, EmptyPage):
            images = paginator.page(paginator.num_pages)

    d = {'archive': archive, 'images': images, 'view': view}

    if view == "edit" and request.user.is_staff:

        paginator = Paginator(archive.image_set.all(), num_images)
        try:
            page = int(request.GET.get("page", '1'))
        except ValueError:
            page = 1

        try:
            all_images = paginator.page(page)
        except (InvalidPage, EmptyPage):
            all_images = paginator.page(paginator.num_pages)

        form = ArchiveModForm(instance=archive)
        image_formset = ImageFormSet(
            queryset=all_images.object_list,
            prefix='images'
        )
        d.update({
            'form': form,
            'image_formset': image_formset,
            'matchers': crawler_settings.provider_context.get_matchers(crawler_settings, force=True),
            'api_key': crawler_settings.api_key,
        })

    if request.user.is_authenticated:
        user_archive_preferences = UserArchivePrefs.objects.filter(
            user=request.user.pk, archive=pk)
        if not user_archive_preferences.exists():
            user_archive_preferences.favorite_group = 0
        else:
            user_archive_preferences = UserArchivePrefs.objects.get(
                user=request.user.pk, archive=pk)
        d.update({'user_archive_preferences': user_archive_preferences})

    # In-place collaborator edit form
    if request.user.has_perm('viewer.change_archive'):
        if request.POST.get('change-archive'):
            # create a form instance and populate it with data from the request:
            edit_form = ArchiveEditForm(request.POST, instance=archive)
            # check whether it's valid:
            if edit_form.is_valid():
                new_archive = edit_form.save()
                message = 'Archive successfully modified'
                messages.success(request, message)
                frontend_logger.info("User {}: {}".format(request.user.username, message))
                event_log(
                    request.user,
                    'CHANGE_ARCHIVE',
                    content_object=new_archive,
                    result='changed'
                )
                # return HttpResponseRedirect(request.META["HTTP_REFERER"])
            else:
                messages.error(request, 'The provided data is not valid', extra_tags='danger')
                # return HttpResponseRedirect(request.META["HTTP_REFERER"])
        else:
            edit_form = ArchiveEditForm(instance=archive)
        d.update({'edit_form': edit_form})

    return render(request, "viewer/archive.html", d)


@login_required
def archive_update(request: HttpRequest, pk: int, tool: str = None, tool_use_id: str = None) -> HttpResponse:
    """Update archive title, rating, tags, archives."""
    if not request.user.is_staff:
        messages.error(request, "You need to be an admin to update an archive.")
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if tool == 'select-as-match':
        archive.select_as_match(tool_use_id)
        if archive.gallery:
            frontend_logger.info("Archive {} ({}) was matched with gallery {} ({}).".format(
                archive,
                reverse('viewer:archive', args=(archive.pk,)),
                archive.gallery,
                reverse('viewer:gallery', args=(archive.gallery.pk,)),
            ))
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == 'clear-possible-matches':
        archive.possible_matches.clear()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    user_archive_preferences = UserArchivePrefs.objects.filter(
        user=request.user.pk, archive=pk)
    if not user_archive_preferences.exists():
        user_archive_preferences.favorite_group = 0
    else:
        user_archive_preferences = UserArchivePrefs.objects.get(
            user=request.user.pk, archive=pk)

    d = {'archive': archive, 'view': "edit", 'user_archive_preferences': user_archive_preferences}

    if request.method == 'POST':
        p = request.POST
        image_formset = ImageFormSet(p,
                                     queryset=archive.image_set.all(),
                                     prefix='images')
        if image_formset.is_valid():
            images = image_formset.save(commit=False)
            for image in images:
                image.save()
            for image in image_formset.deleted_objects:
                image.delete_plus_files()

        archive.title = p["title"]
        archive.title_jpn = p["title_jpn"]
        archive.source_type = p["source_type"]
        archive.reason = p["reason"]
        archive.details = p["details"]

        if "zipped" in p:
            if p["zipped"] != '' and p["zipped"] != archive.zipped:
                result = archive.rename_zipped_pathname(p["zipped"])
                if not result:
                    messages.error(request, "File {} already exists, renaming failed".format(p["zipped"]))

        if "custom_tags" in p:
            lst = []
            tags = p.getlist("custom_tags")
            for t in tags:
                lst.append(Tag.objects.get(pk=t))
            archive.custom_tags.set(lst)
        else:
            archive.custom_tags.clear()
        if "possible_matches" in p and p["possible_matches"] != "":

            matched_gallery = Gallery.objects.get(pk=p["possible_matches"])

            archive.gallery_id = p["possible_matches"]
            archive.title = matched_gallery.title
            archive.title_jpn = matched_gallery.title_jpn
            archive.tags.set(matched_gallery.tags.all())

            archive.match_type = "manual:cutoff"
            archive.possible_matches.clear()

            if 'failed' in matched_gallery.dl_type:
                matched_gallery.dl_type = 'manual:matched'
                matched_gallery.save()
        if "alternative_sources" in p:
            lst = []
            alternative_sources = p.getlist("alternative_sources")
            for alternative_gallery in alternative_sources:
                lst.append(Gallery.objects.get(pk=alternative_gallery))
            archive.alternative_sources.set(lst)
        else:
            archive.alternative_sources.clear()
        archive.simple_save()

        messages.success(request, 'Updated archive: {}'.format(archive.title))

    else:
        image_formset = ImageFormSet(
            queryset=archive.image_set.all(),
            prefix='images'
        )
    form = ArchiveModForm(instance=archive)
    d.update({
        'form': form,
        'image_formset': image_formset,
        'matchers': crawler_settings.provider_context.get_matchers(crawler_settings, force=True),
        'api_key': crawler_settings.api_key,
    })

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


def archive_download(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")
    if not archive.public and not request.user.is_authenticated:
        raise Http404("Archive is not public")
    if 'HTTP_X_FORWARDED_HOST' in request.META:
        response = HttpResponse()
        response["Content-Type"] = "application/zip"
        if 'original' in request.GET:
            response["Content-Disposition"] = 'attachment; filename*=UTF-8\'\'{0}'.format(
                quote(basename(archive.zipped.name)))
        else:
            response["Content-Disposition"] = 'attachment; filename*=UTF-8\'\'{0}'.format(
                archive.pretty_name)
        response['X-Accel-Redirect'] = "/download/{0}".format(quote(archive.zipped.name)).encode('utf-8')
        return response
    else:
        return HttpResponseRedirect(archive.zipped.url)


def archive_ext_download(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")
    if not archive.public and not request.user.is_authenticated:
        raise Http404("Archive is not public")

    if 'original' in request.GET:
        filename = quote(basename(archive.zipped.name))
    else:
        filename = archive.pretty_name

    redirect_url = "{0}/{1}?filename={2}".format(
        crawler_settings.urls.external_media_server,
        quote(archive.zipped.name),
        filename
    ).encode('utf-8')

    return HttpResponseRedirect(redirect_url)


def archive_thumb(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")
    if not archive.public and not request.user.is_authenticated:
        raise Http404("Archive is not public")
    if 'HTTP_X_FORWARDED_HOST' in request.META:
        response = HttpResponse()
        response["Content-Type"] = "image/jpeg"
        # response["Content-Disposition"] = 'attachment; filename*=UTF-8\'\'{0}'.format(
        #         archive.pretty_name)
        response['X-Accel-Redirect'] = "/image/{0}".format(archive.thumbnail.name)
        return response
    else:
        return HttpResponseRedirect(archive.thumbnail.url)


@login_required
def extract_toggle(request: HttpRequest, pk: int) -> HttpResponse:
    """Extract archive toggle."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to toggle extract an archive.")

    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            frontend_logger.info('Toggling images for ' + archive.zipped.name)
            archive.extract_toggle()
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


@login_required
def public_toggle(request: HttpRequest, pk: int) -> HttpResponse:
    """Public archive toggle."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to toggle public access for an archive.")

    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if archive.public:
        archive.set_private()
        frontend_logger.info('Setting public status to: private for ' + archive.zipped.name)
        event_log(
            request.user,
            'UNPUBLISH_ARCHIVE',
            content_object=archive,
            result='unpublished'
        )
    else:
        archive.set_public()
        frontend_logger.info('Setting public status to: public for ' + archive.zipped.name)
        event_log(
            request.user,
            'PUBLISH_ARCHIVE',
            content_object=archive,
            result='published'
        )

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


@login_required
def recalc_info(request: HttpRequest, pk: int) -> HttpResponse:
    """Recalculate archive info."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to recalculate file info.")

    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    frontend_logger.info('Recalculating file info for ' + archive.zipped.name)
    archive.recalc_fileinfo()
    archive.generate_image_set(force=True)
    archive.generate_thumbnails()

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


@login_required
def recall_api(request: HttpRequest, pk: int) -> HttpResponse:
    """Recall provider API, if possible."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to recall the API.")

    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if not archive.gallery_id:
        return render_error(request, "No gallery associated with this archive.")

    gallery = Gallery.objects.get(pk=archive.gallery_id)

    current_settings = Settings(load_from_config=crawler_settings.config)

    if current_settings.workers.web_queue:

        current_settings.set_update_metadata_options(providers=(gallery.provider,))

        current_settings.workers.web_queue.enqueue_args_list((gallery.get_link(),), override_options=current_settings)

        frontend_logger.info(
            'Updating gallery API data for gallery: {} and related archives'.format(
                gallery.get_absolute_url()
            )
        )

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


@login_required
def generate_matches(request: HttpRequest, pk: int) -> HttpResponse:
    """Generate matches for non-match."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to generate matches.")

    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if archive.gallery:
        return render_error(request, "Archive is already matched.")

    clear_title = True if 'clear' in request.GET else False

    provider_filter = request.GET.get('provider', '')
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

    frontend_logger.info('Generated matches for {}, found {}'.format(
        archive.zipped.path,
        archive.possible_matches.count()
    ))

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


@login_required
def rematch_archive(request: HttpRequest, pk: int) -> HttpResponse:
    """Match an Archive."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to rematch an archive.")

    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if archive.gallery:
        archive.gallery.archive_set.remove(archive)

    folder_crawler_thread = FolderCrawlerThread(
        crawler_logger, crawler_settings, ['-frm', archive.zipped.path])
    folder_crawler_thread.start()

    frontend_logger.info('Rematching archive: {}'.format(archive.title))

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


@login_required
def delete_archive(request: HttpRequest, pk: int) -> HttpResponse:
    """Delete archive and gallery data if there is any."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to delete an archive.")

    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if request.method == 'POST':

        p = request.POST
        if "delete_confirm" in p:

            message_list = list()

            if "delete-archive" in p:
                message_list.append('archive entry')
            if "delete-gallery" in p:
                message_list.append('associated gallery')
            if "delete-file" in p:
                message_list.append('associated file')

            message = 'For archive: {}, deleting: {}'.format(archive.title, ', '.join(message_list))

            frontend_logger.info("User {}: {}".format(request.user.username, message))
            messages.success(request, message)

            gallery = archive.gallery

            if "mark-gallery-deleted" in p:
                archive.gallery.mark_as_deleted()
                archive.gallery = None
            if "delete-file" in p:
                archive.delete_all_files()
            if "delete-archive" in p:
                archive.delete_files_but_archive()
                archive.delete()

            user_reason = p.get('reason', '')

            event_log(
                request.user,
                'DELETE_ARCHIVE',
                reason=user_reason,
                content_object=gallery,
                result='deleted'
            )

            return HttpResponseRedirect(reverse('viewer:main-page'))

    d = {'archive': archive}

    return render(request, "viewer/delete_archive.html", d)
