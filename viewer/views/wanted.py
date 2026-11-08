import urllib.parse

import django.utils.timezone as django_tz
import logging
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, InvalidPage, Page, Paginator
from django.db.models import Prefetch
from django.http import Http404, HttpResponseRedirect, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.conf import settings

from core.base.setup import Settings
from viewer.forms import WantedGalleryCreateOrEditForm
from viewer.models import Gallery, WantedGallery, FoundGallery, GalleryMatch, Archive
from viewer.utils.general import clean_up_referer
from viewer.utils.tags import sort_tags
from viewer.utils.actions import event_log
from workers import client


crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)


def _paginate_queryset(queryset, page_number: int | str | None, per_page: int = 25) -> Page:
    paginator = Paginator(queryset, per_page)
    try:
        page_val = int(page_number) if page_number is not None else 1
    except (ValueError, TypeError):
        page_val = 1

    try:
        return paginator.page(page_val)
    except EmptyPage:
        if page_val > paginator.num_pages and paginator.num_pages > 0:
            return paginator.page(paginator.num_pages)
        return paginator.page(1)
    except InvalidPage:
        return paginator.page(1)


@login_required
def wanted_gallery(request: HttpRequest, pk: int) -> HttpResponse:
    """WantedGallery view adapting to user permissions."""
    is_staff = request.user.is_staff
    has_view_perm = request.user.has_perm("viewer.view_wantedgallery")
    has_change_perm = request.user.has_perm("viewer.change_wantedgallery")
    has_add_perm = request.user.has_perm("viewer.add_wantedgallery")
    can_view_internal = is_staff or has_view_perm
    can_change = is_staff or has_change_perm
    can_add = is_staff or has_add_perm

    try:
        wanted_gallery_instance = WantedGallery.objects.prefetch_related(
            'wanted_tags',
            'unwanted_tags',
            'categories',
            'artists',
            'wanted_providers',
            'unwanted_providers',
            'monitoredlink_set',
        ).get(pk=pk)
    except WantedGallery.DoesNotExist:
        raise Http404("Wanted gallery does not exist")

    if not can_view_internal and not wanted_gallery_instance.public:
        raise Http404("Wanted gallery does not exist")

    # Handle edit form submission
    edit_form: WantedGalleryCreateOrEditForm | None = None
    if request.POST.get("submit-wanted-gallery") and can_change:
        edit_form = WantedGalleryCreateOrEditForm(request.POST, instance=wanted_gallery_instance)
        if edit_form.is_valid():
            new_wanted_gallery = edit_form.save()
            message = "Wanted gallery successfully modified"
            messages.success(request, message)
            logger.info("User {}: {}".format(request.user.username, message))
            event_log(request.user, "CHANGE_WANTED_GALLERY", content_object=new_wanted_gallery, result="changed")
        else:
            messages.error(request, "The provided data is not valid", extra_tags="danger")
    elif can_change:
        edit_form = WantedGalleryCreateOrEditForm(instance=wanted_gallery_instance)

    # Handle tools
    tool = request.GET.get("tool", "")
    tool_use_id = request.GET.get("tool-id", "")
    referer = clean_up_referer(request.META.get("HTTP_REFERER", reverse("viewer:wanted-gallery", args=[wanted_gallery_instance.pk])))

    if tool:
        if can_change:
            if tool == "select-as-match":
                try:
                    matched_gallery = Gallery.objects.get(pk=tool_use_id)
                except Gallery.DoesNotExist:
                    raise Http404("Gallery does not exist")
                FoundGallery.objects.get_or_create(wanted_gallery=wanted_gallery_instance, gallery=matched_gallery)
                wanted_gallery_instance.found = True
                wanted_gallery_instance.date_found = django_tz.now()
                gm = GalleryMatch.objects.filter(wanted_gallery=wanted_gallery_instance, gallery=matched_gallery)
                if gm:
                    gm.delete()
                wanted_gallery_instance.save()

                logger.info(
                    "WantedGallery {} ({}) was matched with gallery {} ({}).".format(
                        wanted_gallery_instance,
                        reverse("viewer:wanted-gallery", args=(wanted_gallery_instance.pk,)),
                        matched_gallery,
                        reverse("viewer:gallery", args=(matched_gallery.pk,)),
                    )
                )
                return HttpResponseRedirect(referer)
            elif tool == "remove-match":
                try:
                    matched_gallery = Gallery.objects.get(pk=tool_use_id)
                except Gallery.DoesNotExist:
                    raise Http404("Gallery does not exist")
                fg = FoundGallery.objects.filter(wanted_gallery=wanted_gallery_instance, gallery=matched_gallery)
                if fg:
                    fg.delete()
                gm = GalleryMatch.objects.filter(wanted_gallery=wanted_gallery_instance, gallery=matched_gallery)
                if gm:
                    gm.delete()
                wanted_gallery_instance.save()

                logger.info(
                    "Gallery {} ({}) was removed as a match for WantedGallery {} ({}).".format(
                        matched_gallery,
                        reverse("viewer:gallery", args=(matched_gallery.pk,)),
                        wanted_gallery_instance,
                        reverse("viewer:wanted-gallery", args=(wanted_gallery_instance.pk,)),
                    )
                )
                return HttpResponseRedirect(referer)
            elif tool == "stop-searching":
                wanted_gallery_instance.should_search = False
                wanted_gallery_instance.save()
                return HttpResponseRedirect(referer)
            elif tool == "toggle-public":
                wanted_gallery_instance.public_toggle()
                return HttpResponseRedirect(referer)

        if is_staff:
            if tool == "create-possible-matches-internally":
                provider = request.GET.get("provider", "")
                wanted_gallery_instance.create_gallery_matches_internally(provider_filter=provider)
                logger.info(
                    "Wanted gallery {} ({}) internal gallery match resulted in {} possible matches.".format(
                        wanted_gallery_instance,
                        reverse("viewer:wanted-gallery", args=(wanted_gallery_instance.pk,)),
                        wanted_gallery_instance.possible_matches.count(),
                    )
                )
                return HttpResponseRedirect(referer)
            elif tool == "clear-possible-matches":
                wanted_gallery_instance.possible_matches.clear()
                return HttpResponseRedirect(referer)
            elif tool == "create-matches-internally":
                wanted_gallery_instance.create_found_galleries_internally()
                return HttpResponseRedirect(referer)
            elif tool == "backlog-search":
                urls_to_match = request.GET.getlist("urls-to-match", [])
                url_query = wanted_gallery_instance.backlog_url_query

                current_settings = Settings(load_from_config=crawler_settings.config)

                if urls_to_match and url_query:
                    current_settings.silent_processing = True
                    current_settings.replace_metadata = True
                    current_settings.archive_origin = Archive.ORIGIN_WANTED_GALLERY

                    arguments_to_crawler = [
                        x["url"] + urllib.parse.quote_plus(url_query)
                        for x in current_settings.backlog_search.urls
                        if x["name"] in urls_to_match
                    ]

                    arguments_to_crawler.append("-wanted")
                    current_settings.archive_user = request.user
                    current_settings.event_action = "WANTED_GALLERY_MATCH"

                    client.enqueue(
                        args=arguments_to_crawler,
                        task_type="web_crawler",
                        override_options=current_settings,
                        user=request.user,
                        event_action="WANTED_GALLERY_MATCH",
                    )
                return HttpResponseRedirect(referer)

    def get_page_size(param_name: str, default: int = 25) -> int:
        val = request.GET.get(param_name)
        if val is not None:
            try:
                parsed = int(val)
                return parsed if parsed > 0 else default
            except (ValueError, TypeError):
                pass
        return default

    per_page_default = get_page_size("limit", get_page_size("per_page", 25))
    found_per_page = get_page_size("found_limit", get_page_size("found_per_page", per_page_default))
    matches_per_page = get_page_size("matches_limit", get_page_size("matches_per_page", per_page_default))
    mentions_per_page = get_page_size("mentions_limit", get_page_size("mentions_per_page", per_page_default))

    found_galleries_qs = (
        wanted_gallery_instance.foundgallery_set
        .select_related("gallery")
        .prefetch_related("gallery__archive_set")
        .order_by("-create_date", "-pk")
    )
    found_page = request.GET.get("found_page") or request.GET.get("page_found") or 1
    found_galleries = _paginate_queryset(found_galleries_qs, found_page, found_per_page)

    if can_view_internal:
        matches_qs = (
            wanted_gallery_instance.gallerymatch_set
            .select_related("gallery")
            .prefetch_related("gallery__archive_set")
            .order_by("-match_accuracy", "-pk")
        )
        matches_page = request.GET.get("matches_page") or request.GET.get("page_matches") or 1
        possible_matches = _paginate_queryset(matches_qs, matches_page, matches_per_page)
    else:
        possible_matches = None

    mentions_qs = wanted_gallery_instance.mentions.order_by("-mention_date", "-pk")
    mentions_page = request.GET.get("mentions_page") or request.GET.get("page_mentions") or 1
    mentions = _paginate_queryset(mentions_qs, mentions_page, mentions_per_page)

    d = {
        "wanted_gallery": wanted_gallery_instance,
        "edit_form": edit_form,
        "backlog_search_urls": crawler_settings.backlog_search.urls if is_staff else [],
        "can_change": can_change,
        "can_add": can_add,
        "can_view_internal": can_view_internal,
        "found_galleries": found_galleries,
        "possible_matches": possible_matches,
        "mentions": mentions,
    }
    return render(request, "viewer/wanted_gallery.html", d)
