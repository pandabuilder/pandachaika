import base64
import logging
import mimetypes
import os
import re
from os.path import basename
from urllib.parse import quote, urlparse, unquote
import typing

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.views import redirect_to_login
from django.contrib.contenttypes.models import ContentType
from django.core.paginator import Paginator, InvalidPage, EmptyPage, Page
from django.urls import reverse
from django.db import transaction
from django.db.models import Sum
from django.http import Http404, HttpRequest
from django.http import HttpResponseRedirect, HttpResponse, FileResponse, StreamingHttpResponse, HttpResponseBase
from django.utils.http import content_disposition_header
from django.templatetags.static import static
from django.views.decorators.http import require_POST
from django.shortcuts import render
from django.conf import settings

from workers import client
from viewer.utils.actions import event_log
from viewer.forms import ArchiveModForm, ImageFormSet, ArchiveEditForm, ArchiveManageEditFormSet, SplitArchiveFormSet
from viewer.models import (
    Archive,
    Tag,
    Gallery,
    Image,
    ItemProperties,
    UserArchivePrefs,
    ArchiveManageEntry,
    ArchiveRecycleEntry,
    GalleryProviderData,
    ArchiveGroup,
    ArchiveGroupEntry,
    ArchiveTag,
    ArchiveOption,
    ArchiveStatistics,
    UserLongLivedToken,
)
from viewer.utils.general import clean_up_referer, should_use_refined_views
from viewer.utils.requests import double_check_auth, authenticate_by_token
from viewer.views.head import render_error
from viewer.services.archive_extraction import ArchiveExtractionService

logger = logging.getLogger(__name__)
crawler_settings = settings.CRAWLER_SETTINGS


archive_download_regex = re.compile(r"/archive/(\d+)/download/$", re.IGNORECASE)


VALID_ARCHIVE_VIEW_MODES = ("cover", "thumbnails", "full", "single")


def archive_details(request: HttpRequest, pk: int, mode: str = "view") -> HttpResponse:
    """Archive listing."""

    try:
        archive = Archive.objects.select_related("gallery").get(pk=pk)
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

    d: dict[str, typing.Any] = {"archive": archive, "view": view, "mode": mode}

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
                page = int(request.GET.get("page", "1"))
            except ValueError:
                page = 1

            try:
                images_page: Page | None = paginator.page(page)
            except (InvalidPage, EmptyPage):
                images_page = paginator.page(paginator.num_pages)

        else:
            images_page = None

        d.update({"images": images_page})

    if mode == "edit" and request.user.is_staff:

        paginator = Paginator(archive.image_set.all(), num_images)
        try:
            page = int(request.GET.get("page", "1"))
        except ValueError:
            page = 1

        try:
            all_images = paginator.page(page)
        except (InvalidPage, EmptyPage):
            all_images = paginator.page(paginator.num_pages)

        form = ArchiveModForm(instance=archive, initial={"archive_groups": archive.archive_groups.all()})
        page_image_pks = [img.pk for img in all_images]
        image_formset = ImageFormSet(
            queryset=archive.image_set.filter(pk__in=page_image_pks),
            prefix="images",
        )
        d.update(
            {
                "form": form,
                "image_formset": image_formset,
                "image_queryset": all_images,
                "matchers": crawler_settings.provider_context.get_matchers(crawler_settings, force=True),
            }
        )

    if request.user.is_authenticated:
        current_user_archive_preferences, created = UserArchivePrefs.objects.get_or_create(
            user__id=request.user.pk,
            archive=archive,
            defaults={"user_id": request.user.pk, "archive": archive, "favorite_group": 0},
        )
        manga_extractor = getattr(crawler_settings, "manga_extractor", None)
        me_enabled = bool(manga_extractor and manga_extractor.enable)
        me_profiles = {}
        default_profile_name = "default"
        if me_enabled and manga_extractor:
            me_profiles = manga_extractor.get_profile_choices()
            default_profile_name = manga_extractor.default_profile

        d.update(
            {
                "user_archive_preferences": current_user_archive_preferences,
                "is_extracting": (not archive.extracted) and client.is_archive_extracting(archive.pk),
                "is_reducing": archive.extracted and client.is_archive_reducing(archive.pk),
                "is_publishing": (not archive.public) and client.is_archive_publishing(archive.pk),
                "is_extracting_context": client.is_archive_extracting_context(archive.pk),
                "manga_extractor_enabled": me_enabled,
                "manga_extractor_profiles": me_profiles,
                "manga_extractor_default_profile": default_profile_name,
            }
        )

    # In-place collaborator edit form
    if request.user.has_perm("viewer.change_archive"):
        if request.POST.get("change-archive"):
            # create a form instance and populate it with data from the request:
            old_gallery = archive.gallery
            edit_form = ArchiveEditForm(request.POST, instance=archive)
            # check whether it's valid:
            if edit_form.is_valid():
                new_archive: Archive = edit_form.save(commit=False)
                new_archive.simple_save()
                edit_form.save_m2m()

                if edit_form.cleaned_data["freeze_titles"]:
                    new_archive.do_freeze_titles()
                else:
                    new_archive.undo_freeze_titles()

                if edit_form.cleaned_data["freeze_tags"]:
                    new_archive.do_freeze_tags()
                else:
                    new_archive.undo_freeze_tags()

                if new_archive.gallery:
                    if new_archive.gallery.tags.all():
                        new_archive.set_tags_from_gallery(new_archive.gallery)
                    if new_archive.gallery != old_gallery:
                        new_archive.set_titles_from_gallery(new_archive.gallery, dont_save=True)
                        if edit_form.cleaned_data["old_gallery_to_alt"] and old_gallery is not None:
                            new_archive.alternative_sources.add(old_gallery)
                        new_archive.simple_save()
                        edit_form = ArchiveEditForm(instance=new_archive)

                message = "Archive successfully modified"
                messages.success(request, message)
                logger.info("User {}: {}".format(request.user.username, message))
                event_log(request.user, "CHANGE_ARCHIVE", content_object=new_archive, result="changed")
                # return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
            else:
                messages.error(request, "The provided data is not valid", extra_tags="danger")
                # return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
        else:
            archive_option = ArchiveOption.objects.filter(archive=archive).first()
            if not archive_option:
                freeze_titles = False
                freeze_tags = False
            else:
                freeze_titles = archive_option.freeze_titles
                freeze_tags = archive_option.freeze_tags
            edit_form = ArchiveEditForm(
                initial={"freeze_titles": freeze_titles, "freeze_tags": freeze_tags}, instance=archive
            )
        d.update({"edit_form": edit_form})

    if request.user.has_perm("viewer.mark_archive") and request.user.is_authenticated:
        if request.POST.get("manage-archive"):
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
                        event_log(request.user, "MARK_ARCHIVE", content_object=archive, result="created")
                    else:
                        event_log(request.user, "MARK_ARCHIVE", content_object=archive, result="modified")
                    manage_instance.save()
                for manage_instance in archive_manage_formset.deleted_objects:
                    if manage_instance.mark_user != request.user and not request.user.is_staff:
                        messages.error(
                            request, "Cannot delete the specified mark, you are not the owner", extra_tags="danger"
                        )
                    else:
                        event_log(request.user, "MARK_ARCHIVE", content_object=archive, result="deleted")
                        manage_instance.delete()

                messages.success(request, "Successfully modified Archive manage data")
                archive_manage_formset = ArchiveManageEditFormSet(
                    instance=archive, queryset=ArchiveManageEntry.objects.filter(mark_user=request.user),
                )
            else:
                messages.error(request, "The provided data is not valid", extra_tags="danger")
        else:
            archive_manage_formset = ArchiveManageEditFormSet(
                instance=archive, queryset=ArchiveManageEntry.objects.filter(mark_user=request.user),
            )

        d.update({"archive_manage_formset": archive_manage_formset})

    if request.user.has_perm("viewer.view_marks"):
        manage_entries = ArchiveManageEntry.objects.filter(archive=archive)
        d.update({"manage_entries": manage_entries, "manage_entries_count": manage_entries.count()})
    elif crawler_settings.urls.enable_public_marks and crawler_settings.urls.public_mark_reasons:
        manage_entries = ArchiveManageEntry.objects.filter(
            archive=archive, mark_reason__in=crawler_settings.urls.public_mark_reasons,
            status=ArchiveManageEntry.StatusChoices.NORMAL
        )
        d.update({"manage_entries": manage_entries})

    archive_tags = list(archive.archivetag_set.all())
    tag_count = sum(1 for at in archive_tags if at.origin != ArchiveTag.ORIGIN_USER)
    custom_tag_count = len(archive_tags) - tag_count
    file_entry_total = archive.archivefileentry_set.aggregate(total=Sum("file_size"))["total"] or 0

    d.update(
        {
            "tag_count": tag_count,
            "custom_tag_count": custom_tag_count,
            "file_entry_total": file_entry_total,
            # 'public_mark_reasons': crawler_settings.urls.public_mark_reasons
        }
    )

    if archive.gallery:
        gallery_provider_data = GalleryProviderData.objects.filter(gallery=archive.gallery)
        d.update({"gallery_provider_data": gallery_provider_data})

    archive_ct = ContentType.objects.get_for_model(Archive)
    d.update(
        {
            "archive_statistics": ArchiveStatistics.objects.filter(archive=archive).first(),
            "archive_contextual_properties": ItemProperties.objects.filter(
                content_type=archive_ct, object_id=archive.pk, tag="manga-extractor"
            ).order_by("name"),
        }
    )

    template = (
        "viewer/archive_new.html"
        if should_use_refined_views(request)
        else "viewer/archive_old.html"
    )
    return render(request, template, d)


@login_required
def archive_update(
    request: HttpRequest, pk: int, tool: str | None = None, tool_use_id: str | None = None
) -> HttpResponse:
    """Update archive title, rating, tags, archives."""
    if not request.user.is_staff:
        messages.error(request, "You need to be an admin to update an archive.")
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if tool == "select-as-match" and tool_use_id:
        try:
            gallery_id = int(tool_use_id)
            archive.select_as_match(gallery_id)
            if archive.gallery:
                logger.info(
                    "Archive {} ({}) was matched with gallery {} ({}).".format(
                        archive,
                        reverse("viewer:archive", args=(archive.pk,)),
                        archive.gallery,
                        reverse("viewer:gallery", args=(archive.gallery.pk,)),
                    )
                )
        except ValueError:
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "clear-possible-matches":
        archive.possible_matches.clear()
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

    current_user_archive_preferences, created = UserArchivePrefs.objects.get_or_create(
        user__id=request.user.pk,
        archive=archive,
        defaults={"user_id": request.user.pk, "archive": archive, "favorite_group": 0},
    )

    d = {"archive": archive, "view": "edit", "user_archive_preferences": current_user_archive_preferences}

    if request.method == "POST":
        p = request.POST
        image_formset = ImageFormSet(p, queryset=archive.image_set.all(), prefix="images")
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
            if p["zipped"] != "" and p["zipped"] != archive.zipped:
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
            archive.set_titles_from_gallery(matched_gallery, dont_save=True)
            archive.set_tags_from_gallery(matched_gallery)

            archive.match_type = "manual:cutoff"
            archive.possible_matches.clear()

            if "failed" in matched_gallery.dl_type:
                matched_gallery.dl_type = "manual:matched"
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

        messages.success(request, "Updated archive: {}".format(archive.title))

    else:
        image_formset = ImageFormSet(queryset=archive.image_set.all(), prefix="images")
    form = ArchiveModForm(instance=archive)
    d.update(
        {
            "form": form,
            "image_formset": image_formset,
            "matchers": crawler_settings.provider_context.get_matchers(crawler_settings, force=True),
        }
    )

    return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))


def archive_auth(request: HttpRequest) -> HttpResponse:
    archive_url = request.META.get("HTTP_X_ORIGINAL_URI", None)
    if archive_url is None:
        return HttpResponse(status=403)

    archive_parts = urlparse(archive_url)

    archive_path = unquote(archive_parts.path).removeprefix("/download/")
    try:
        archive = Archive.objects.get(zipped=archive_path)
    except Archive.DoesNotExist:
        return HttpResponse(status=403)
    if not archive.public and not request.user.is_authenticated:
        return HttpResponse(status=403)
    return HttpResponse(status=200)


@login_required
def archive_enter_reason(request: HttpRequest, pk: int, tool: str | None = None) -> HttpResponse:
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")
    if not request.user.is_authenticated:
        raise Http404("Archive does not exist")

    if request.method == "POST":

        p = request.POST
        user_reason = p.get("reason", "")
        if "confirm_tool" in p:

            if request.user.has_perm("viewer.recycle_archive") and tool == "recycle":

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
                            "MOVE_TO_RECYCLE_BIN",
                            content_object=archive,
                            result="recycled",
                            reason=user_reason,
                        )
                        if archive.public:
                            archive.set_private()
                            event_log(request.user, "UNPUBLISH_ARCHIVE", content_object=archive, result="unpublished")

                return HttpResponseRedirect(archive.get_absolute_url())

            elif request.user.has_perm("viewer.recycle_archive") and tool == "unrecycle":

                if archive.is_recycled():
                    with transaction.atomic():
                        r = archive.recycle_entry
                        r.delete()
                        archive.binned = False
                        archive.simple_save()
                        event_log(
                            request.user,
                            "RESTORE_FROM_RECYCLE_BIN",
                            content_object=archive,
                            result="restored",
                            reason=user_reason,
                        )

                return HttpResponseRedirect(archive.get_absolute_url())

    d = {"archive": archive, "tool": tool}

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

    token_valid, token_user = authenticate_by_token(request, UserLongLivedToken.SCOPE_READ)

    user_is_authenticated = request.user.is_authenticated or token_valid
    
    if not archive.public and not user_is_authenticated:
        raise Http404("Archive does not exist")
    if not archive.zipped.name:
        raise Http404("Archive does not exist")
    if "HTTP_X_FORWARDED_HOST" in request.META:
        response = HttpResponse()
        response["Content-Type"] = "application/vnd.comicbook+zip"
        if "original" in request.GET:
            response["Content-Disposition"] = "attachment; filename*=UTF-8''{0}".format(
                quote(basename(archive.zipped.name))
            )
        else:
            response["Content-Disposition"] = "attachment; filename*=UTF-8''{0}".format(archive.pretty_name)
        response["X-Accel-Redirect"] = "/download/{0}".format(quote(archive.zipped.name)).encode("utf-8")
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
    if not archive.zipped.name:
        raise Http404("Archive does not exist")

    if "original" in request.GET:
        filename = quote(basename(archive.zipped.name))
    else:
        filename = archive.pretty_name

    redirect_url = "{0}/{1}?filename={2}".format(
        crawler_settings.urls.external_media_server, quote(archive.zipped.name), filename
    )

    return HttpResponseRedirect(redirect_url)


def archive_thumb(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")
    if not archive.public and not request.user.is_authenticated:
        raise Http404("Archive does not exist")

    thumb = archive.thumbnail
    try:
        has_thumb = bool(thumb and thumb.name)
    except ValueError:
        has_thumb = False

    if not has_thumb and archive.gallery:
        try:
            if archive.gallery.thumbnail and archive.gallery.thumbnail.name:
                thumb = archive.gallery.thumbnail
                has_thumb = True
        except ValueError:
            has_thumb = False

    if not has_thumb:
        return HttpResponseRedirect(static("imgs/no_cover.png"))

    if "HTTP_X_FORWARDED_HOST" in request.META:
        response = HttpResponse()
        response["Content-Type"] = "image/jpeg"
        # response["Content-Disposition"] = 'attachment; filename*=UTF-8\'\'{0}'.format(
        #         archive.pretty_name)
        response["X-Accel-Redirect"] = "/image/{0}".format(quote(thumb.name or ""))
        return response
    else:
        try:
            return HttpResponseRedirect(thumb.url)
        except ValueError:
            return HttpResponseRedirect(static("imgs/no_cover.png"))


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
        page = int(request.GET.get("page", "1"))
    except ValueError:
        page = 1

    try:
        results: Page | None = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    has_contextual_props = False
    if results and results.object_list:
        image_ct = ContentType.objects.get_for_model(Image)
        image_pks = [img.pk for img in results.object_list if img.pk]
        props_by_img: dict[int, list[ItemProperties]] = {}
        for prop in ItemProperties.objects.filter(
            content_type=image_ct, object_id__in=image_pks, tag="manga-extractor"
        ).order_by("name"):
            if prop.name == "bubble_transcripts":
                parsed = prop.parsed_json
                if isinstance(parsed, list):
                    setattr(prop, "bubble_list", parsed)
            props_by_img.setdefault(prop.object_id, []).append(prop)
        for img in results.object_list:
            img_props = props_by_img.get(img.pk, [])
            setattr(img, "contextual_properties", img_props)
            if img_props:
                has_contextual_props = True

    d = {"archive": archive, "results": results, "has_contextual_props": has_contextual_props}

    return render(request, "viewer/archive_image_data_list.html", d)


@permission_required("viewer.read_archive_change_log")
def change_log(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")
    if not archive.public and not request.user.is_authenticated:
        raise Http404("Archive does not exist")

    archive_history = archive.history.order_by("-history_date")

    paginator = Paginator(archive_history, 400)
    try:
        page = int(request.GET.get("page", "1"))
    except ValueError:
        page = 1

    try:
        results: Page | None = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    if results is not None:
        history_list = list(results.object_list)
        for i in range(len(history_list) - 1):
            history_list[i].cached_prev_record = history_list[i + 1]
        if history_list:
            history_list[-1].cached_prev_record = history_list[-1].prev_record
        results.object_list = history_list

    d = {"archive": archive, "results": results}

    return render(request, "viewer/archive_change_log.html", d)


def image_live_thumb(request: HttpRequest, archive_pk: int, position: int) -> HttpResponse:
    try:
        image = Image.objects.get(archive=archive_pk, position=position)
    except Image.DoesNotExist:
        raise Http404("Archive does not exist")
    if not double_check_auth(request, UserLongLivedToken.SCOPE_READ)[0]:
        raise Http404("Archive does not exist")

    full_image = bool(request.GET.get("full", ""))

    image_data = image.fetch_image_data(use_original_image=full_image)
    if not image_data:
        return HttpResponse("")

    if request.GET.get("base64", ""):
        image_data_enconded = "data:image/jpeg;base64," + base64.b64encode(image_data).decode("utf-8")
        response = HttpResponse(image_data_enconded)
        response["Cache-Control"] = "max-age=86400"
    else:
        response = HttpResponse(image_data)
        response["Content-Type"] = "image/jpeg"
        response["Cache-Control"] = "max-age=86400"
    return response


def _serve_image(request: HttpRequest, pk: int, as_attachment: bool = True) -> HttpResponseBase:
    """Serve an individual image in its original resolution (only for authenticated users)."""
    is_auth, auth_user = double_check_auth(request, UserLongLivedToken.SCOPE_READ)
    if not is_auth:
        return redirect_to_login(request.get_full_path())

    try:
        image = Image.objects.select_related("archive").get(pk=pk)
    except Image.DoesNotExist:
        raise Http404("Image does not exist")

    archive = image.archive
    if not archive.public and not is_auth:
        raise Http404("Archive does not exist")

    if archive.is_recycled() or archive.is_file_deleted():
        raise Http404("Archive does not exist")

    # If the file is extracted and it's not "resized", serve directly from filesystem
    if image.is_extracted_and_not_resized():
        img_name = image.image.name if image.image and image.image.name else ""
        filename: str = (
            image.image_name or (os.path.basename(img_name) if img_name else f"image_{image.position}.jpg")
        )
        guessed_type = mimetypes.guess_type(filename)[0]
        content_type: str = guessed_type if guessed_type is not None else "application/octet-stream"

        if "HTTP_X_FORWARDED_HOST" in request.META:
            forwarded_response = HttpResponse()
            forwarded_response["Content-Type"] = content_type
            disposition = content_disposition_header(as_attachment, filename)
            if disposition:
                forwarded_response["Content-Disposition"] = disposition
            forwarded_response["X-Accel-Redirect"] = f"/image/{quote(img_name)}"
            return forwarded_response
        else:
            file_response = FileResponse(open(image.image.path, "rb"), as_attachment=as_attachment, filename=filename)
            file_response["Content-Type"] = content_type
            return file_response

    # Otherwise, dynamically access it from the original .zip file and stream it back
    stream_info = ArchiveExtractionService.get_image_zip_stream(image)
    if not stream_info:
        if image.extracted and image.image and os.path.isfile(image.image.path):
            img_name = image.image.name if image.image and image.image.name else ""
            filename = (
                image.image_name or (os.path.basename(img_name) if img_name else f"image_{image.position}.jpg")
            )
            guessed_type = mimetypes.guess_type(filename)[0]
            fallback_content_type: str = guessed_type if guessed_type is not None else "application/octet-stream"
            fallback_response = FileResponse(open(image.image.path, "rb"), as_attachment=as_attachment, filename=filename)
            fallback_response["Content-Type"] = fallback_content_type
            return fallback_response
        raise Http404("Image file does not exist")

    stream_generator, filename, zip_content_type, file_size = stream_info
    stream_content_type: str = zip_content_type if zip_content_type is not None else "application/octet-stream"
    streaming_response = StreamingHttpResponse(stream_generator(), content_type=stream_content_type)
    stream_disposition = content_disposition_header(as_attachment, filename)
    if stream_disposition:
        streaming_response["Content-Disposition"] = stream_disposition
    if file_size is not None:
        streaming_response["Content-Length"] = str(file_size)
    return streaming_response


def image_download(request: HttpRequest, pk: int) -> HttpResponseBase:
    """Download an individual image in its original resolution (only for authenticated users)."""
    return _serve_image(request, pk, as_attachment=True)


def image_view(request: HttpRequest, pk: int) -> HttpResponseBase:
    """View an individual image inline in its original resolution (only for authenticated users)."""
    return _serve_image(request, pk, as_attachment=False)


def archive_image_download(request: HttpRequest, archive_pk: int, position: int) -> HttpResponseBase:
    """Download an individual image by archive pk and position in original resolution."""
    try:
        image = Image.objects.get(archive_id=archive_pk, position=position)
    except Image.DoesNotExist:
        raise Http404("Image does not exist")
    return image_download(request, image.pk)


def archive_image_view(request: HttpRequest, archive_pk: int, position: int) -> HttpResponseBase:
    """View an individual image inline by archive pk and position in original resolution."""
    try:
        image = Image.objects.get(archive_id=archive_pk, position=position)
    except Image.DoesNotExist:
        raise Http404("Image does not exist")
    return image_view(request, image.pk)


@login_required
def extract(request: HttpRequest, pk: int) -> HttpResponse:
    """Extract archive (idempotent, queued to background worker)."""

    if not request.user.has_perm("viewer.expand_archive"):
        return render_error(request, "You don't have the permission to expand an Archive.")
    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            target_url = clean_up_referer(request.META.get("HTTP_REFERER", archive.get_absolute_url()))
            if archive.extracted:
                logger.info("Archive %s is already extracted.", archive.pk)
                return HttpResponseRedirect(target_url)

            if client.is_archive_extracting(archive.pk):
                logger.info("Archive %s extraction is already queued or in progress.", archive.pk)
                messages.info(request, "Archive extraction is already queued or in progress.")
                return HttpResponseRedirect(target_url)

            resized = bool(request.GET.get("resized", ""))

            if resized:
                logger.info("Queueing image expansion (resized) for archive: {}".format(archive.get_absolute_url()))
            else:
                logger.info("Queueing image expansion for archive: {}".format(archive.get_absolute_url()))

            client.enqueue(
                args=[archive.pk],
                task_type="extract_worker",
                override_options={"resized": resized},
                user=request.user,
                event_action="EXPAND_ARCHIVE",
            )
            messages.success(
                request,
                "Queued archive extraction (resized)." if resized else "Queued archive extraction.",
            )
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(target_url)


@login_required
def reduce(request: HttpRequest, pk: int) -> HttpResponse:
    """Reduce archive (idempotent, queued to background worker)."""

    if not request.user.has_perm("viewer.expand_archive"):
        return render_error(request, "You don't have the permission to expand an Archive.")
    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            target_url = clean_up_referer(request.META.get("HTTP_REFERER", archive.get_absolute_url()))
            if not archive.extracted:
                logger.info("Archive %s is already reduced.", archive.pk)
                return HttpResponseRedirect(target_url)

            if client.is_archive_reducing(archive.pk):
                logger.info("Archive %s reduction is already queued or in progress.", archive.pk)
                messages.info(request, "Archive reduction is already queued or in progress.")
                return HttpResponseRedirect(target_url)

            logger.info("Queueing image reduction for archive: {}".format(archive.get_absolute_url()))

            client.enqueue(
                args=[archive.pk],
                task_type="reduce_worker",
                user=request.user,
                event_action="REDUCE_ARCHIVE",
            )
            messages.success(request, "Queued archive reduction.")
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(target_url)


@login_required
def check_and_convert_filetype(request: HttpRequest, pk: int) -> HttpResponse:
    """Check and convert archive filetype (queued to background worker)."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to run this tool.")
    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            target_url = clean_up_referer(request.META.get("HTTP_REFERER", archive.get_absolute_url()))

            if client.is_archive_task_active(archive.pk, "convert_archive_worker"):
                messages.info(request, "Archive filetype conversion is already queued or in progress.")
                return HttpResponseRedirect(target_url)

            logger.info("Queueing filetype check and conversion to ZIP for archive: {}".format(archive.get_absolute_url()))
            client.enqueue(
                args=[archive.pk],
                task_type="convert_archive_worker",
                user=request.user,
                event_action="CONVERT_TO_ZIP",
            )
            messages.success(request, "Queued archive filetype check and conversion.")
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(target_url)


@login_required
def clone_plus(request: HttpRequest, pk: int) -> HttpResponse:
    """Clone and apply other tools (reorder by sha1 list, etc, queued to background worker)."""

    if not request.user.has_perm("viewer.modify_archive_tools"):
        return render_error(request, "You don't have the permission to modify the underlying file.")
    if not request.user.is_authenticated:
        raise Http404("Archive does not exist")

    p = request.POST

    if p and "submit" in p:

        reorder_by_sha1 = p.get("reorder-by-sha1", "")
        run_image_tool = p.get("run-image-tool", "")
        bin_original = p.get("bin-original", "")

        if run_image_tool and not crawler_settings.cloning_image_tool.enable:
            messages.error(request, "Clone image tool is not setup.")
            return HttpResponseRedirect(clean_up_referer(request.META.get("HTTP_REFERER", reverse("viewer:archive", args=[pk]))))

        if reorder_by_sha1:
            sha1_text = p.get("sha1s", "")
            sha1_list = sha1_text.splitlines()
        else:
            sha1_list = None

        user_reason = p.get("reason", "")

        try:
            with transaction.atomic():
                archive = Archive.objects.select_for_update().get(pk=pk)
                target_url = clean_up_referer(request.META.get("HTTP_REFERER", archive.get_absolute_url()))

                if client.is_archive_task_active(archive.pk, "clone_archive_worker"):
                    messages.info(request, "Archive cloning is already queued or in progress.")
                    return HttpResponseRedirect(target_url)

                logger.info("Queueing clone for Archive: {}".format(archive.get_absolute_url()))

                options = {
                    "sha1_list": sha1_list,
                    "run_image_tool": bool(run_image_tool),
                    "bin_original": bool(bin_original),
                    "user_reason": user_reason,
                }
                client.enqueue(
                    args=[archive.pk],
                    task_type="clone_archive_worker",
                    override_options=options,
                    user=request.user,
                    reason=user_reason,
                    event_action="CLONE_ARCHIVE",
                )
                messages.success(request, "Queued archive clone operation.")
        except Archive.DoesNotExist:
            raise Http404("Archive does not exist")

        return HttpResponseRedirect(target_url)
    else:

        try:
            archive = Archive.objects.get(pk=pk)
        except Archive.DoesNotExist:
            raise Http404("Archive does not exist")

        d = {
            "archive": archive,
            "image_tool": crawler_settings.cloning_image_tool,
        }

        inlined = request.GET.get("inline", None)

        if inlined:
            return render(request, "viewer/include/archive_clone_plus.html", d)
        else:
            return render(request, "viewer/archive_display_clone_plus.html", d)


@login_required
def split(request: HttpRequest, pk: int) -> HttpResponse:
    """Split an Archive into several others (queued to background worker)."""

    if not request.user.has_perm("viewer.modify_archive_tools"):
        return render_error(request, "You don't have the permission to modify the underlying file.")
    if not request.user.is_authenticated:
        raise Http404("Archive does not exist")

    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    p = request.POST

    if p and "submit" in p:

        bin_original = p.get("bin-original", "")
        user_reason = p.get("reason", "")
        split_nested = p.get("split-nested", "")

        target_url = clean_up_referer(request.META.get("HTTP_REFERER", archive.get_absolute_url()))

        if client.is_archive_task_active(archive.pk, "split_archive_worker"):
            messages.info(request, "Archive split is already queued or in progress.")
            return HttpResponseRedirect(target_url)

        if split_nested:
            logger.info("Queueing split (nested) for Archive: {}".format(archive.get_absolute_url()))
            options = {
                "cleaned_data": [],
                "split_nested": True,
                "bin_original": bool(bin_original),
                "user_reason": user_reason,
            }
            client.enqueue(
                args=[archive.pk],
                task_type="split_archive_worker",
                override_options=options,
                user=request.user,
                reason=user_reason,
                event_action="SPLIT_ARCHIVE",
            )
            messages.success(request, "Queued archive split operation (nested).")
            return HttpResponseRedirect(target_url)
        else:
            split_form = SplitArchiveFormSet(
                p, queryset=Archive.objects.none(), prefix="archives", filecount=archive.filecount
            )
            if split_form.is_valid():
                cleaned_data = [
                    (x["starting_position"], x["ending_position"], x["new_file_name"], False)
                    for x in split_form.cleaned_data
                    if x
                ]
                logger.info("Queueing split for Archive: {}".format(archive.get_absolute_url()))
                options = {
                    "cleaned_data": cleaned_data,
                    "split_nested": False,
                    "bin_original": bool(bin_original),
                    "user_reason": user_reason,
                }
                client.enqueue(
                    args=[archive.pk],
                    task_type="split_archive_worker",
                    override_options=options,
                    user=request.user,
                    reason=user_reason,
                    event_action="SPLIT_ARCHIVE",
                )
                messages.success(request, "Queued archive split operation.")
                return HttpResponseRedirect(target_url)
            else:
                messages.error(request, "The provided data is not valid", extra_tags="danger")
    else:
        split_form = SplitArchiveFormSet(
            queryset=Archive.objects.none(), prefix="archives", filecount=archive.filecount
        )

    d = {
        "archive": archive,
        "split_form": split_form,
    }

    inlined = request.GET.get("inline", None)

    if inlined:
        return render(request, "viewer/include/archive_split.html", d)
    else:
        return render(request, "viewer/archive_display_split.html", d)


@login_required
@require_POST
def public(request: HttpRequest, pk: int) -> HttpResponse:
    """Public archive (idempotent, queued to background worker)."""

    if not request.user.has_perm("viewer.publish_archive"):
        return render_error(request, "You don't have the permission to public an Archive.")

    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            target_url = clean_up_referer(request.META.get("HTTP_REFERER", archive.get_absolute_url()))
            if archive.public:
                return render_error(request, "Archive is already public.")

            if client.is_archive_publishing(archive.pk):
                logger.info("Archive %s publishing is already queued or in progress.", archive.pk)
                messages.info(request, "Archive publishing is already queued or in progress.")
                return HttpResponseRedirect(target_url)

            logger.info("Queueing public status for Archive: {}".format(archive.get_absolute_url()))
            client.enqueue(
                args=[archive.pk],
                task_type="publish_worker",
                user=request.user,
                event_action="PUBLISH_ARCHIVE",
            )
            messages.success(request, "Queued archive publishing.")
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(target_url)


@login_required
@require_POST
def private(request: HttpRequest, pk: int) -> HttpResponse:
    """Private archive."""

    if not request.user.has_perm("viewer.publish_archive"):
        return render_error(request, "You don't have the permission to private an Archive.")

    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            if not archive.public:
                return render_error(request, "Archive is already private.")
            archive.set_private()
            logger.info("Setting public status to private for Archive: {}".format(archive.get_absolute_url()))
            event_log(request.user, "UNPUBLISH_ARCHIVE", content_object=archive, result="unpublished")
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))


@login_required
@require_POST
def calculate_images_sha1(request: HttpRequest, pk: int) -> HttpResponse:
    """Calculate archive's images SHA1 (queued to background worker)."""

    if not request.user.has_perm("viewer.publish_archive"):
        return render_error(request, "You don't have the permission to calculate SHA1.")

    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            target_url = clean_up_referer(request.META.get("HTTP_REFERER", archive.get_absolute_url()))

            if client.is_archive_task_active(archive.pk, "calculate_sha1_worker"):
                messages.info(request, "Archive images SHA1 calculation is already queued or in progress.")
                return HttpResponseRedirect(target_url)

            logger.info("Queueing images SHA1 calculation for Archive: {}".format(archive.get_absolute_url()))
            client.enqueue(
                args=[archive.pk],
                task_type="calculate_sha1_worker",
                user=request.user,
                event_action="CALCULATE_IMAGES_SHA1",
            )
            messages.success(request, "Queued images SHA1 calculation.")
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(target_url)


@login_required
@require_POST
def recalc_info(request: HttpRequest, pk: int) -> HttpResponse:
    """Recalculate archive info (queued to background worker)."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to recalculate file info.")

    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            target_url = clean_up_referer(request.META.get("HTTP_REFERER", archive.get_absolute_url()))

            if client.is_archive_task_active(archive.pk, "fileinfo_worker"):
                messages.info(request, "Archive file info recalculation is already queued or in progress.")
                return HttpResponseRedirect(target_url)

            logger.info("Queueing file info recalculation for Archive: {}".format(archive.get_absolute_url()))
            client.enqueue(
                args=[archive.pk],
                task_type="fileinfo_worker",
                override_options={"full": True},
                user=request.user,
                event_action="RECALCULATE_FILEINFO",
            )
            messages.success(request, "Queued archive file info recalculation.")
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(target_url)


@login_required
@require_POST
def extract_context(request: HttpRequest, pk: int) -> HttpResponse:
    """Enqueue contextual extraction via manga-extractor for a single archive."""
    if not (request.user.is_staff or request.user.has_perm("viewer.extract_contextual_info")):
        return render_error(request, "You don't have the permission to extract contextual information.")

    manga_extractor = getattr(crawler_settings, "manga_extractor", None)
    if not manga_extractor or not manga_extractor.enable:
        return render_error(request, "Manga extractor is not enabled in settings.")

    profile_name = request.POST.get("profile") or manga_extractor.default_profile

    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            target_url = clean_up_referer(request.META.get("HTTP_REFERER", archive.get_absolute_url()))

            if client.is_archive_extracting_context(archive.pk):
                logger.info("Archive %s contextual extraction is already queued or in progress.", archive.pk)
                messages.info(request, "Contextual extraction is already queued or in progress.")
                return HttpResponseRedirect(target_url)

            logger.info("Queueing contextual extraction for Archive: %s (profile: %s)", archive.get_absolute_url(), profile_name)
            client.enqueue_contextual_extraction(
                [archive.pk],
                user=request.user,
                event_action="EXTRACT_CONTEXTUAL_INFO",
                profile=profile_name,
            )
            messages.success(request, f"Queued contextual extraction ({profile_name}).")
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(target_url)


@login_required
def mark_similar_archives(request: HttpRequest, pk: int) -> HttpResponse:
    """Create similar info as marks for archive (queued to background worker)."""

    if not request.user.has_perm("viewer.mark_similar_archive"):
        return render_error(request, "You don't have the permission to mark similar archives.")

    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            target_url = clean_up_referer(request.META.get("HTTP_REFERER", archive.get_absolute_url()))

            if client.is_archive_task_active(archive.pk, "mark_similar_worker"):
                messages.info(request, "Archive similarity check is already queued or in progress.")
                return HttpResponseRedirect(target_url)

            use_recycled_archives = bool(request.GET.get("use-recycled", ""))

            logger.info("Queueing mark similar archives for Archive: {}, use recycled archives: {}".format(archive.get_absolute_url(), use_recycled_archives))
            client.enqueue(
                args=[archive.pk],
                task_type="mark_similar_worker",
                override_options={"use_recycled": use_recycled_archives},
                user=request.user,
                event_action="MARK_SIMILAR",
            )
            messages.success(request, "Queued archive similarity check.")
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(target_url)


@login_required
@require_POST
def recall_api(request: HttpRequest, pk: int) -> HttpResponse:
    """Recall provider API, if possible."""

    if not request.user.has_perm("viewer.update_metadata"):
        return render_error(request, "You don't have the permission to refresh source metadata on an Archive.")

    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if not archive.gallery_id:
        return render_error(request, "No gallery associated with this archive.")

    gallery = Gallery.objects.get(pk=archive.gallery_id)

    if gallery.provider:
        client.enqueue_gallery_update(
            [gallery.get_link()],
            user=request.user,
        )

        logger.info("Updating gallery API data for gallery: {} and related archives".format(gallery.get_absolute_url()))

    return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))


@login_required
def generate_matches(request: HttpRequest, pk: int) -> HttpResponse:
    """Generate matches for non-match (queued to background worker)."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to generate matches.")

    try:
        with transaction.atomic():
            archive = Archive.objects.select_for_update().get(pk=pk)
            target_url = clean_up_referer(request.META.get("HTTP_REFERER", archive.get_absolute_url()))

            if archive.gallery:
                return render_error(request, "Archive is already matched.")

            if client.is_archive_task_active(archive.pk, "match_unmatched_worker"):
                messages.info(request, "Archive match generation is already queued or in progress.")
                return HttpResponseRedirect(target_url)

            clear_title = True if "clear" in request.GET else False

            provider_filter = request.GET.get("provider", "")
            try:
                cutoff = float(request.GET.get("cutoff", "0.4"))
            except ValueError:
                cutoff = 0.4
            try:
                max_matches = int(request.GET.get("max-matches", "10"))
            except ValueError:
                max_matches = 10

            logger.info("Queueing generate matches for Archive: {}".format(archive.get_absolute_url()))
            options = {
                "clear_title": clear_title,
                "provider_filter": provider_filter,
                "cutoff": cutoff,
                "max_matches": max_matches,
                "auto_link": "auto_link" in request.GET,
            }
            client.enqueue(
                args=[archive.pk],
                task_type="match_unmatched_worker",
                override_options=options,
                user=request.user,
                event_action="GENERATE_MATCHES",
            )
            messages.success(request, "Queued match generation for archive.")
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    return HttpResponseRedirect(target_url)


@login_required
@require_POST
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

    client.enqueue(
        args=["-frm", archive.zipped.path],
        task_type="folder_crawler",
        override_options=crawler_settings,
        user=request.user,
    )

    logger.info("Rematching archive: {}".format(archive.title))

    return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))


@login_required
def delete_archive(request: HttpRequest, pk: int) -> HttpResponse:
    """Delete archive and gallery data if there is any (queued to background worker)."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to delete an archive.")

    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if request.method == "POST":

        p = request.POST
        if "delete_confirm" in p:

            message_list = list()

            # Delete zipped takes priority, deletes actual file but preserves image data
            if "delete-zipped" in p:
                message_list.append("associated zipped file only")
            else:
                if "delete-archive" in p:
                    message_list.append("archive entry")
                if "delete-file" in p:
                    message_list.append("associated file")
            if "delete-gallery" in p:
                message_list.append("associated gallery")

            message = "For archive: {}, queued deletion of: {}".format(archive.title, ", ".join(message_list))

            logger.info("User {}: {}".format(request.user.username, message))
            messages.success(request, message)

            options = {
                "delete_zipped": "delete-zipped" in p,
                "delete_archive": "delete-archive" in p,
                "delete_file": "delete-file" in p,
                "delete_gallery": "delete-gallery" in p,
                "mark_gallery_deleted": "mark-gallery-deleted" in p,
                "mark_gallery_denied": "mark-gallery-denied" in p,
                "user_reason": p.get("reason", ""),
            }

            client.enqueue(
                args=[archive.pk],
                task_type="delete_archive_worker",
                override_options=options,
                user=request.user,
                reason=p.get("reason", ""),
                event_action="DELETE_ARCHIVE",
            )

            return HttpResponseRedirect(reverse("viewer:main-page"))

    d = {"archive": archive}

    inlined = request.GET.get("inline", None)

    if inlined:
        return render(request, "viewer/include/delete_archive.html", d)
    else:
        return render(request, "viewer/archive_display_delete.html", d)


@login_required
@require_POST
def delete_manage_archive(request: HttpRequest, pk: int) -> HttpResponse:
    """Delete Manage Archive entry."""

    if not request.user.has_perm("viewer.mark_archive"):
        return render_error(request, "You don't have the permission to mark an Archive.")

    try:
        archive_manage_entry = ArchiveManageEntry.objects.get(pk=pk)
    except ArchiveManageEntry.DoesNotExist:
        messages.error(request, "ArchiveManageEntry does not exist")
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

    if archive_manage_entry.mark_user != request.user and not request.user.is_staff:
        return render_error(request, "You don't have the permission to delete this mark.")

    messages.success(request, "Deleting ArchiveManageEntry for Archive: {}".format(archive_manage_entry.archive))

    event_log(request.user, "DELETE_MANAGER_ARCHIVE", content_object=archive_manage_entry.archive, result="deleted")

    archive_manage_entry.delete()

    return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))


@login_required
@require_POST
def remove_from_index_manage_archive(request: HttpRequest, pk: int) -> HttpResponse:
    """Mark Archive entry as non-searchable."""

    if not request.user.has_perm("viewer.edit_system_marks"):
        return render_error(request, "You don't have the permission to remove from index a Mark for an Archive.")
    try:
        archive_manage_entry = ArchiveManageEntry.objects.get(pk=pk)
    except ArchiveManageEntry.DoesNotExist:
        messages.error(request, "ArchiveManageEntry does not exist")
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

    if (archive_manage_entry.mark_user is not None and archive_manage_entry.mark_user.id != 1) and not request.user.is_staff:
        return render_error(request, "You don't have the permission to remove from index a Mark for an Archive.")

    messages.success(request, "Removing from index ArchiveManageEntry for Archive: {}".format(archive_manage_entry.archive))

    event_log(request.user, "REMOVE_FROM_INDEX_MANAGER_ARCHIVE", content_object=archive_manage_entry.archive, result="remove_from_index")

    archive_manage_entry.status = ArchiveManageEntry.StatusChoices.NOT_INDEXED
    archive_manage_entry.save()

    return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

@login_required
@require_POST
def add_to_index_manage_archive(request: HttpRequest, pk: int) -> HttpResponse:
    """Mark Archive entry as searchable."""

    if not request.user.has_perm("viewer.edit_system_marks"):
        return render_error(request, "You don't have the permission to add to index a Mark for an Archive.")
    try:
        archive_manage_entry = ArchiveManageEntry.objects.get(pk=pk)
    except ArchiveManageEntry.DoesNotExist:
        messages.error(request, "ArchiveManageEntry does not exist")
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

    if (archive_manage_entry.mark_user is not None and archive_manage_entry.mark_user.id != 1) and not request.user.is_staff:
        return render_error(request, "You don't have the permission to add to index a Mark for an Archive.")

    messages.success(request, "Adding to index ArchiveManageEntry for Archive: {}".format(archive_manage_entry.archive))

    event_log(request.user, "ADD_TO_INDEX_MANAGER_ARCHIVE", content_object=archive_manage_entry.archive, result="add_to_index")

    archive_manage_entry.status = ArchiveManageEntry.StatusChoices.NORMAL
    archive_manage_entry.save()

    return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))