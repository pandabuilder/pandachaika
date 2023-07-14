# Here are the views for interacting with the archive Model
# exclusively.

import logging
import re
from os.path import basename
from urllib.parse import quote, urlparse, unquote
import typing
from typing import Optional
import base64

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator, InvalidPage, EmptyPage, Page
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
    ArchiveEditForm, ArchiveManageEditFormSet)
from viewer.models import (
    Archive, Tag, Gallery, Image,
    UserArchivePrefs, ArchiveManageEntry, ArchiveRecycleEntry, GalleryProviderData, ArchiveGroup, ArchiveGroupEntry,
    ArchiveTag
)
from viewer.utils.requests import double_check_auth
from viewer.views.head import render_error

logger = logging.getLogger(__name__)
crawler_settings = settings.CRAWLER_SETTINGS


archive_download_regex = re.compile(r"/archive/(\d+)/download/$", re.IGNORECASE)


VALID_ARCHIVE_VIEW_MODES = ('cover', 'thumbnails', 'full', 'single')


def archive_details(request: HttpRequest, pk: int, mode: str = 'view') -> HttpResponse:
    """Archive listing."""

    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")
    if not archive.public and not request.user.is_authenticated:
        raise Http404("Archive does not exist")

    if mode == "edit" and not request.user.is_staff:
        raise Http404("Archive does not exist")

    if request.user.is_authenticated:
        view = request.GET.get("view", None)
        if view is not None and view in VALID_ARCHIVE_VIEW_MODES:
            request.session["archive_view"] = view
        else:
            if "archive_view" in request.session:
                view = request.session["archive_view"]
            else:
                view = "cover"
    else:
        view = "cover"

    d: dict[str, typing.Any] = {'archive': archive, 'view': view, 'mode': mode}

    num_images = 30
    if view == "full":
        num_images = 10
    if view in ("single", "cover"):
        num_images = 1

    if request.user.is_authenticated:

        images = archive.image_set.filter(extracted=True)

        if images:
            paginator = Paginator(images, num_images)
            try:
                page = int(request.GET.get("page", '1'))
            except ValueError:
                page = 1

            try:
                images_page: typing.Optional[Page] = paginator.page(page)
            except (InvalidPage, EmptyPage):
                images_page = paginator.page(paginator.num_pages)

        else:
            images_page = None

        d.update({'images': images_page})

    if mode == "edit" and request.user.is_staff:

        paginator = Paginator(archive.image_set.all(), num_images)
        try:
            page = int(request.GET.get("page", '1'))
        except ValueError:
            page = 1

        try:
            all_images = paginator.page(page)
        except (InvalidPage, EmptyPage):
            all_images = paginator.page(paginator.num_pages)

        form = ArchiveModForm(instance=archive, initial={'archive_groups': archive.archive_groups.all()})
        image_formset = ImageFormSet(
            queryset=all_images.object_list,  # type: ignore
            prefix='images'
        )
        d.update({
            'form': form,
            'image_formset': image_formset,
            'image_queryset': all_images,
            'matchers': crawler_settings.provider_context.get_matchers(crawler_settings, force=True),
        })

    if request.user.is_authenticated:
        current_user_archive_preferences, created = UserArchivePrefs.objects.get_or_create(
            user__id=request.user.pk,
            archive=archive,
            defaults={'user_id': request.user.pk, 'archive': archive, 'favorite_group': 0}
        )
        d.update({'user_archive_preferences': current_user_archive_preferences})

    # In-place collaborator edit form
    if request.user.has_perm('viewer.change_archive'):
        if request.POST.get('change-archive'):
            # create a form instance and populate it with data from the request:
            old_gallery = archive.gallery
            edit_form = ArchiveEditForm(request.POST, instance=archive)
            # check whether it's valid:
            if edit_form.is_valid():
                # TODO: Maybe this should be in save method for the form
                new_archive: Archive = edit_form.save(commit=False)
                new_archive.simple_save()
                edit_form.save_m2m()
                if new_archive.gallery:
                    if new_archive.gallery.tags.all():
                        new_archive.set_tags_from_gallery(new_archive.gallery)
                    if new_archive.gallery != old_gallery:
                        new_archive.title = new_archive.gallery.title
                        new_archive.title_jpn = new_archive.gallery.title_jpn
                        if edit_form.cleaned_data['old_gallery_to_alt'] and old_gallery is not None:
                            new_archive.alternative_sources.add(old_gallery)
                        new_archive.simple_save()
                        edit_form = ArchiveEditForm(instance=new_archive)

                message = 'Archive successfully modified'
                messages.success(request, message)
                logger.info("User {}: {}".format(request.user.username, message))
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

    if request.user.has_perm('viewer.mark_archive') and request.user.is_authenticated:
        if request.POST.get('manage-archive'):
            archive_manage_formset = ArchiveManageEditFormSet(request.POST, instance=archive)
            # check whether it's valid:
            if archive_manage_formset.is_valid():

                archive_manages = archive_manage_formset.save(commit=False)
                for manage_instance in archive_manages:
                    if manage_instance.pk is None:
                        manage_instance.archive = archive
                        manage_instance.mark_check = True
                        manage_instance.mark_user = request.user
                        manage_instance.origin = ArchiveManageEntry.ORIGIN_USER
                        event_log(
                            request.user,
                            'MARK_ARCHIVE',
                            content_object=archive,
                            result='created'
                        )
                    else:
                        event_log(
                            request.user,
                            'MARK_ARCHIVE',
                            content_object=archive,
                            result='modified'
                        )
                    manage_instance.save()
                for manage_instance in archive_manage_formset.deleted_objects:
                    if manage_instance.mark_user != request.user and not request.user.is_staff:
                        messages.error(request, 'Cannot delete the specified mark, you are not the owner', extra_tags='danger')
                    else:
                        event_log(
                            request.user,
                            'MARK_ARCHIVE',
                            content_object=archive,
                            result='deleted'
                        )
                        manage_instance.delete()

                messages.success(request, 'Sucessfully modified Archive manage data')
                archive_manage_formset = ArchiveManageEditFormSet(instance=archive, queryset=ArchiveManageEntry.objects.filter(mark_user=request.user))
            else:
                messages.error(request, 'The provided data is not valid', extra_tags='danger')
        else:
            archive_manage_formset = ArchiveManageEditFormSet(instance=archive, queryset=ArchiveManageEntry.objects.filter(mark_user=request.user))

        d.update({'archive_manage_formset': archive_manage_formset})

    if request.user.has_perm('viewer.view_marks'):
        manage_entries = ArchiveManageEntry.objects.filter(archive=archive)
        d.update({'manage_entries': manage_entries, 'manage_entries_count': manage_entries.count()})

    d.update(
        {
            'tag_count': archive.tags.exclude(archivetag__origin=ArchiveTag.ORIGIN_USER).count(),
            'custom_tag_count': archive.tags.filter(archivetag__origin=ArchiveTag.ORIGIN_USER).count()
        }
    )

    if archive.gallery:
        gallery_provider_data = GalleryProviderData.objects.filter(gallery=archive.gallery)
        d.update({'gallery_provider_data': gallery_provider_data})

    return render(request, "viewer/archive.html", d)


@login_required
def archive_update(request: HttpRequest, pk: int, tool: Optional[str] = None, tool_use_id: Optional[str] = None) -> HttpResponse:
    """Update archive title, rating, tags, archives."""
    if not request.user.is_staff:
        messages.error(request, "You need to be an admin to update an archive.")
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if tool == 'select-as-match' and tool_use_id:
        try:
            gallery_id = int(tool_use_id)
            archive.select_as_match(gallery_id)
            if archive.gallery:
                logger.info("Archive {} ({}) was matched with gallery {} ({}).".format(
                    archive,
                    reverse('viewer:archive', args=(archive.pk,)),
                    archive.gallery,
                    reverse('viewer:gallery', args=(archive.gallery.pk,)),
                ))
        except ValueError:
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == 'clear-possible-matches':
        archive.possible_matches.clear()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    current_user_archive_preferences, created = UserArchivePrefs.objects.get_or_create(
        user__id=request.user.pk,
        archive=archive,
        defaults={'user_id': request.user.pk, 'archive': archive, 'favorite_group': 0}
    )

    d = {'archive': archive, 'view': "edit", 'user_archive_preferences': current_user_archive_preferences}

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

            # Force relative positions
            for count, image in enumerate(archive.image_set.all(), start=1):
                image.position = count
                # image.archive_position = count
                image.save()

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

        if "tags" in p:
            archive_custom_tags = ArchiveTag.objects.filter(archive=archive, origin=ArchiveTag.ORIGIN_USER)
            archive_custom_tags.delete()
            tags = p.getlist("tags")
            for t in tags:
                tag = Tag.objects.get(pk=t)
                archive_custom_tag = ArchiveTag(archive=archive, tag=tag, origin=ArchiveTag.ORIGIN_USER)
                archive_custom_tag.save()

        else:
            archive_custom_tags = ArchiveTag.objects.filter(archive=archive, origin=ArchiveTag.ORIGIN_USER)
            archive_custom_tags.delete()
        if "possible_matches" in p and p["possible_matches"] != "":

            matched_gallery = Gallery.objects.get(pk=p["possible_matches"])

            archive.gallery_id = p["possible_matches"]
            archive.title = matched_gallery.title
            archive.title_jpn = matched_gallery.title_jpn
            archive.set_tags_from_gallery(matched_gallery)

            archive.match_type = "manual:cutoff"
            archive.possible_matches.clear()

            if 'failed' in matched_gallery.dl_type:
                matched_gallery.dl_type = 'manual:matched'
                matched_gallery.save()
        if "alternative_sources" in p:
            alternative_sources = p.getlist("alternative_sources")
            alt_galleries = Gallery.objects.filter(pk__in=alternative_sources)
            archive.alternative_sources.set(alt_galleries)
        else:
            archive.alternative_sources.clear()
        if "archive_groups" in p:

            archive_groups_pks = p.getlist("archive_groups")

            current_ages = ArchiveGroupEntry.objects.filter(archive=archive)

            for current_age in current_ages:
                if str(current_age.pk) not in archive_groups_pks:
                    current_age.delete()

            for archive_group_pk in archive_groups_pks:
                if not ArchiveGroupEntry.objects.filter(archive=archive, archive_group__pk=archive_group_pk).exists():
                    archive_group = ArchiveGroup.objects.filter(pk=archive_group_pk).first()

                    if archive_group:
                        archive_group_entry = ArchiveGroupEntry(archive=archive, archive_group=archive_group)
                        archive_group_entry.save()
        else:
            archive.archive_groups.clear()
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
    })

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


def archive_auth(request: HttpRequest) -> HttpResponse:
    archive_url = request.META.get('HTTP_X_ORIGINAL_URI', None)
    if archive_url is None:
        return HttpResponse(status=403)

    archive_parts = urlparse(archive_url)

    archive_path = unquote(archive_parts.path).removeprefix('/download/')
    try:
        archive = Archive.objects.get(zipped=archive_path)
    except Archive.DoesNotExist:
        return HttpResponse(status=403)
    if not archive.public and not request.user.is_authenticated:
        return HttpResponse(status=403)
    return HttpResponse(status=200)


@login_required
def archive_enter_reason(request: HttpRequest, pk: int, tool: Optional[str] = None) -> HttpResponse:
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")
    if not request.user.is_authenticated:
        raise Http404("Archive does not exist")

    if request.method == 'POST':

        p = request.POST
        user_reason = p.get('reason', '')
        if "confirm_tool" in p:

            if request.user.has_perm('viewer.recycle_archive') and tool == "recycle":

                if not archive.is_recycled():
                    with transaction.atomic():
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
                            content_object=archive,
                            result='recycled',
                            reason=user_reason
                        )
                        if archive.public:
                            archive.set_private()
                            event_log(
                                request.user,
                                'UNPUBLISH_ARCHIVE',
                                content_object=archive,
                                result='unpublished'
                            )

                return HttpResponseRedirect(archive.get_absolute_url())

            elif request.user.has_perm('viewer.recycle_archive') and tool == "unrecycle":

                if archive.is_recycled():
                    with transaction.atomic():
                        r = archive.recycle_entry
                        r.delete()
                        archive.binned = False
                        archive.simple_save()
                        event_log(
                            request.user,
                            'RESTORE_FROM_RECYCLE_BIN',
                            content_object=archive,
                            result='restored',
                            reason=user_reason
                        )

                return HttpResponseRedirect(archive.get_absolute_url())

    d = {'archive': archive, 'tool': tool}

    inlined = request.GET.get("inline", None)

    if inlined:
        return render(request, "viewer/include/modals/archive_tool_reason.html", d)
    else:
        return render(request, "viewer/archive_display_tool.html", d)


def archive_download(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")
    if not archive.public and not request.user.is_authenticated:
        raise Http404("Archive does not exist")
    if 'HTTP_X_FORWARDED_HOST' in request.META:
        response = HttpResponse()
        response["Content-Type"] = "application/vnd.comicbook+zip"
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
        raise Http404("Archive does not exist")

    if 'original' in request.GET:
        filename = quote(basename(archive.zipped.name))
    else:
        filename = archive.pretty_name

    redirect_url = "{0}/{1}?filename={2}".format(
        crawler_settings.urls.external_media_server,
        quote(archive.zipped.name),
        filename
    )

    return HttpResponseRedirect(redirect_url)


def archive_thumb(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")
    if not archive.public and not request.user.is_authenticated:
        raise Http404("Archive does not exist")
    if 'HTTP_X_FORWARDED_HOST' in request.META:
        response = HttpResponse()
        response["Content-Type"] = "image/jpeg"
        # response["Content-Disposition"] = 'attachment; filename*=UTF-8\'\'{0}'.format(
        #         archive.pretty_name)
        response['X-Accel-Redirect'] = "/image/{0}".format(archive.thumbnail.name)
        return response
    else:
        return HttpResponseRedirect(archive.thumbnail.url)


def image_data_list(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")
    if not archive.public and not request.user.is_authenticated:
        raise Http404("Archive does not exist")

    images = archive.image_set.all()

    paginator = Paginator(images, 400)
    try:
        page = int(request.GET.get("page", '1'))
    except ValueError:
        page = 1

    try:
        results: typing.Optional[Page] = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    d = {'archive': archive, 'results': results}

    return render(request, "viewer/archive_image_data_list.html", d)


@permission_required('viewer.read_archive_change_log')
def change_log(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")
    if not archive.public and not request.user.is_authenticated:
        raise Http404("Archive does not exist")

    archive_history = archive.history.order_by('-history_date')

    paginator = Paginator(archive_history, 400)
    try:
        page = int(request.GET.get("page", '1'))
    except ValueError:
        page = 1

    try:
        results: typing.Optional[Page] = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    d = {'archive': archive, 'results': results}

    return render(request, "viewer/archive_change_log.html", d)


def image_live_thumb(request: HttpRequest, archive_pk: int, position: int) -> HttpResponse:
    try:
        image = Image.objects.get(archive=archive_pk, position=position)
    except Image.DoesNotExist:
        raise Http404("Archive does not exist")
    if not image.archive.public and not double_check_auth(request)[0]:
        raise Http404("Archive does not exist")

    full_image = bool(request.GET.get("full", ''))

    image_data = image.fetch_image_data(use_original_image=full_image)
    if not image_data:
        return HttpResponse('')

    if request.GET.get("base64", ''):
        image_data_enconded = "data:image/jpeg;base64," + base64.b64encode(image_data).decode('utf-8')
        response = HttpResponse(image_data_enconded)
        response['Cache-Control'] = "max-age=86400"
    else:
        response = HttpResponse(image_data)
        response["Content-Type"] = "image/jpeg"
        response['Cache-Control'] = "max-age=86400"
    return response


@login_required
def extract_toggle(request: HttpRequest, pk: int) -> HttpResponse:
    """Extract archive toggle."""

    if not request.user.has_perm('viewer.expand_archive'):
        return render_error(request, "You don't have the permission to expand an Archive.")
    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            logger.info('Toggling images for archive: {}'.format(archive.get_absolute_url()))
            archive.extract_toggle()
            if archive.extracted:
                action = 'EXPAND_ARCHIVE'
            else:
                action = 'REDUCE_ARCHIVE'
            event_log(
                request.user,
                action,
                content_object=archive,
                result='success'
            )
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


@login_required
def extract(request: HttpRequest, pk: int) -> HttpResponse:
    """Extract archive toggle."""

    if not request.user.has_perm('viewer.expand_archive'):
        return render_error(request, "You don't have the permission to expand an Archive.")
    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            if archive.extracted:
                return render_error(request, "Archive is already extracted.")

            resized = bool(request.GET.get("resized", ''))

            if resized:
                logger.info('Expanding images (resized) for archive: {}'.format(archive.get_absolute_url()))
            else:
                logger.info('Expanding images for archive: {}'.format(archive.get_absolute_url()))

            archive.extract(resized=resized)
            action = 'EXPAND_ARCHIVE'
            event_log(
                request.user,
                action,
                content_object=archive,
                result='success'
            )
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


@login_required
def reduce(request: HttpRequest, pk: int) -> HttpResponse:
    """Reduce archive."""

    if not request.user.has_perm('viewer.expand_archive'):
        return render_error(request, "You don't have the permission to expand an Archive.")
    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            if not archive.extracted:
                return render_error(request, "Archive is already reduced.")

            logger.info('Reducing images for archive: {}'.format(archive.get_absolute_url()))

            archive.reduce()
            action = 'REDUCE_ARCHIVE'
            event_log(
                request.user,
                action,
                content_object=archive,
                result='success'
            )
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


@login_required
def check_and_convert_filetype(request: HttpRequest, pk: int) -> HttpResponse:
    """Check and convert archive filetype."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to run this tool.")
    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)

            logger.info('Checking if archive: {} needs conversion to ZIP.'.format(archive.get_absolute_url()))

            extension, result = archive.check_and_convert_to_zip()

            # Leave up to the user to rename or not, same with recalc
            # if extension in ['rar', '7z'] and result == 2:
            #     base_name, current_extension = splitext(archive.zipped.path)
            #     archive.rename_zipped_pathname(base_name + ".zip")

            if result == 2:
                result_message = 'success'
            elif result == 0:
                result_message = 'skipped'
            elif result == 1:
                result_message = 'failed'
            else:
                result_message = 'unknown'

            event_log(
                request.user,
                'CONVERT_TO_ZIP',
                content_object=archive,
                result=result_message,
                data=extension
            )
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


@login_required
def clone_plus(request: HttpRequest, pk: int) -> HttpResponse:
    """CLone and apply other tools (reorder by sha1 list, etc)."""

    if not request.user.has_perm('viewer.modify_archive_tools'):
        return render_error(request, "You don't have the permission to modify the underlying file.")
    if not request.user.is_authenticated:
        raise Http404("Archive does not exist")

    p = request.POST

    if p and "submit" in p:

        reorder_by_sha1 = p.get("reorder-by-sha1", "")
        run_image_tool = p.get("run-image-tool", "")
        bin_original = p.get("bin-original", "")

        tools_used = []
        
        if run_image_tool and not crawler_settings.cloning_image_tool.enable:
            messages.error(
                request, 'Clone image tool is not setup.'
            )
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
        
        if reorder_by_sha1:
            sha1_text = p.get("sha1s", "")
            sha1_list = sha1_text.splitlines()
            tools_used.append("Reordering based on SHA1 list")
        else:
            sha1_list = None
            
        if run_image_tool:
            tools_used.append("Running Image Tool")

        user_reason = p.get('reason', '')

        try:
            with transaction.atomic():
                archive = Archive.objects.select_for_update().get(pk=pk)

                original_archive_url = request.build_absolute_uri(archive.get_absolute_url())

                logger.info('Cloning Archive: {} and {}.'.format(archive.get_absolute_url(), tools_used))
                
                if run_image_tool:

                    image_tool = crawler_settings.cloning_image_tool if crawler_settings.cloning_image_tool.enable else None

                    new_archive, error_message = archive.clone_archive_plus(sha1_list, image_tool)
                elif sha1_list:
                    new_archive, error_message = archive.create_new_archive_ordered_by_sha1(sha1_list)
                else:
                    new_archive, error_message = archive.clone_archive_plus(sha1_list)

                if new_archive:
                    result_message = 'success'
                    event_log(
                        request.user,
                        'CLONE_ARCHIVE',
                        reason=user_reason,
                        content_object=new_archive,
                        result=result_message,
                        data=original_archive_url
                    )

                    if bin_original and not archive.is_recycled():
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
                            content_object=archive,
                            result='recycled',
                            reason=user_reason
                        )
                        if archive.public:
                            archive.set_private()
                            event_log(
                                request.user,
                                'UNPUBLISH_ARCHIVE',
                                content_object=archive,
                                result='unpublished'
                            )

                    messages.success(
                        request, 'Cloned new Archive: {}'.format(
                            request.build_absolute_uri(new_archive.get_absolute_url())
                        )
                    )
                else:
                    result_message = 'failed'
                    event_log(
                        request.user,
                        'CLONE_ARCHIVE',
                        reason=user_reason,
                        content_object=archive,
                        result=result_message,
                        data=error_message,
                    )
                    messages.error(
                        request, 'Could not clone Archive: {}, error: {}.'.format(
                            request.build_absolute_uri(archive.get_absolute_url()), error_message
                        )
                    )

        except Archive.DoesNotExist:
            raise Http404("Archive does not exist")

        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    else:

        try:
            archive = Archive.objects.get(pk=pk)
        except Archive.DoesNotExist:
            raise Http404("Archive does not exist")

        d = {
            'archive': archive,
            'image_tool': crawler_settings.cloning_image_tool,
        }

        inlined = request.GET.get("inline", None)

        if inlined:
            return render(request, "viewer/include/archive_clone_plus.html", d)
        else:
            return render(request, "viewer/archive_display_clone_plus.html", d)


@login_required
def public(request: HttpRequest, pk: int) -> HttpResponse:
    """Public archive."""

    if not request.user.has_perm('viewer.publish_archive'):
        return render_error(request, "You don't have the permission to public an Archive.")

    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            if archive.public:
                return render_error(request, "Archive is already public.")
            archive.set_public()
            logger.info('Setting public status to public for Archive: {}'.format(archive.get_absolute_url()))
            event_log(
                request.user,
                'PUBLISH_ARCHIVE',
                content_object=archive,
                result='published'
            )
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


@login_required
def private(request: HttpRequest, pk: int) -> HttpResponse:
    """Private archive."""

    if not request.user.has_perm('viewer.publish_archive'):
        return render_error(request, "You don't have the permission to private an Archive.")

    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            if not archive.public:
                return render_error(request, "Archive is already private.")
            archive.set_private()
            logger.info('Setting public status to private for Archive: {}'.format(archive.get_absolute_url()))
            event_log(
                request.user,
                'UNPUBLISH_ARCHIVE',
                content_object=archive,
                result='unpublished'
            )
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


@login_required
def calculate_images_sha1(request: HttpRequest, pk: int) -> HttpResponse:
    """Calculate archive's images SHA1."""

    # TODO: Different permission
    if not request.user.has_perm('viewer.publish_archive'):
        return render_error(request, "You don't have the permission to calculate SHA1.")

    try:
        archive: Archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    logger.info('Calculating images SHA1 for Archive: {}'.format(archive.get_absolute_url()))
    archive.calculate_sha1_and_data_for_images()

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

    logger.info('Recalculating file info for Archive: {}'.format(archive.get_absolute_url()))
    archive.recalc_fileinfo()
    archive.generate_image_set(force=False)
    archive.fix_image_positions()
    archive.generate_thumbnails()

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


@login_required
def mark_similar_archives(request: HttpRequest, pk: int) -> HttpResponse:
    """Create similar info as marks for archive."""

    if not request.user.has_perm('viewer.mark_similar_archive'):
        return render_error(request, "You don't have the permission to mark similar archives.")

    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    logger.info('Creating similar info as marks for Archive: {}'.format(archive.get_absolute_url()))
    archive.create_marks_for_similar_archives()

    if "HTTP_REFERER" in request.META:
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    else:
        return HttpResponseRedirect(reverse('viewer:archive', args=[str(pk)]))


@login_required
def recall_api(request: HttpRequest, pk: int) -> HttpResponse:
    """Recall provider API, if possible."""

    if not request.user.has_perm('viewer.update_metadata'):
        return render_error(request, "You don't have the permission to refresh source metadata on an Archive.")

    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if not archive.gallery_id:
        return render_error(request, "No gallery associated with this archive.")

    gallery = Gallery.objects.get(pk=archive.gallery_id)

    current_settings = Settings(load_from_config=crawler_settings.config)

    if current_settings.workers.web_queue and gallery.provider:

        current_settings.set_update_metadata_options(providers=(gallery.provider,))

        def gallery_callback(x: Optional['Gallery'], crawled_url: Optional[str], result: str) -> None:
            event_log(
                request.user,
                'UPDATE_METADATA',
                content_object=x,
                result=result,
                data=crawled_url
            )

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

    logger.info('Generated matches for {}, found {}'.format(
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
        crawler_settings, ['-frm', archive.zipped.path])
    folder_crawler_thread.start()

    logger.info('Rematching archive: {}'.format(archive.title))

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

            logger.info("User {}: {}".format(request.user.username, message))
            messages.success(request, message)

            gallery = archive.gallery
            archive_report = archive.delete_text_report()
            old_gallery_link = None

            user_reason = p.get('reason', '')

            # Mark deleted takes priority over delete
            if "mark-gallery-deleted" in p and archive.gallery:
                archive.gallery.mark_as_deleted()
                archive.gallery = None

                event_log(
                    request.user,
                    'MARK_DELETE_GALLERY',
                    reason=user_reason,
                    content_object=gallery,
                    result='success',
                )

            elif "delete-gallery" in p and archive.gallery:
                old_gallery_link = archive.gallery.get_link()
                archive.gallery.delete()
                archive.gallery = None
                event_log(
                    request.user,
                    'DELETE_GALLERY',
                    reason=user_reason,
                    data=old_gallery_link,
                    result='deleted',
                )
            if "delete-file" in p:
                archive.delete_all_files()
            if "delete-archive" in p:
                archive.delete_files_but_archive()
                archive.delete()

            if old_gallery_link:
                event_log(
                    request.user,
                    'DELETE_ARCHIVE',
                    reason=user_reason,
                    result='deleted',
                    data=archive_report
                )
            else:
                event_log(
                    request.user,
                    'DELETE_ARCHIVE',
                    reason=user_reason,
                    content_object=gallery,
                    result='deleted',
                    data=archive_report
                )

            return HttpResponseRedirect(reverse('viewer:main-page'))

    d = {'archive': archive}

    inlined = request.GET.get("inline", None)

    if inlined:
        return render(request, "viewer/include/delete_archive.html", d)
    else:
        return render(request, "viewer/archive_display_delete.html", d)


@login_required
def delete_manage_archive(request: HttpRequest, pk: int) -> HttpResponse:
    """Recalculate archive info."""

    if not request.user.has_perm('viewer.mark_archive'):
        return render_error(request, "You don't have the permission to mark an Archive.")

    try:
        archive_manage_entry = ArchiveManageEntry.objects.get(pk=pk)
    except ArchiveManageEntry.DoesNotExist:
        messages.error(request, "ArchiveManageEntry does not exist")
        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    if archive_manage_entry.mark_user != request.user and not request.user.is_staff:
        return render_error(request, "You don't have the permission to delete this mark.")

    messages.success(request, 'Deleting ArchiveManageEntry for Archive: {}'.format(archive_manage_entry.archive))

    event_log(
        request.user,
        'DELETE_MANAGER_ARCHIVE',
        content_object=archive_manage_entry.archive,
        result='deleted'
    )

    archive_manage_entry.delete()

    return HttpResponseRedirect(request.META["HTTP_REFERER"])
