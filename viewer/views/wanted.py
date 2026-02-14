import django.utils.timezone as django_tz
import logging
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.http import Http404, HttpResponseRedirect, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.conf import settings

from viewer.models import Gallery, WantedGallery, FoundGallery, GalleryMatch
from viewer.utils.general import clean_up_referer
from viewer.utils.tags import sort_tags


crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)


@login_required
def wanted_gallery(request: HttpRequest, pk: int) -> HttpResponse:
    """WantedGallery listing."""
    if not request.user.is_staff:
        try:
            wanted_gallery_instance = WantedGallery.objects.get(pk=pk, public=True)
        except WantedGallery.DoesNotExist:
            raise Http404("Wanted gallery does not exist")
        wanted_tag_lists = sort_tags(wanted_gallery_instance.wanted_tags.all())
        unwanted_tag_lists = sort_tags(wanted_gallery_instance.unwanted_tags.all())

        d = {
            "wanted_gallery": wanted_gallery_instance,
            "wanted_tag_lists": wanted_tag_lists,
            "unwanted_tag_lists": unwanted_tag_lists,
        }
    else:
        tool = request.GET.get("tool", "")
        tool_use_id = request.GET.get("tool-id", "")
        try:
            wanted_gallery_instance = WantedGallery.objects.prefetch_related(
                Prefetch(
                    'foundgallery_set',
                    queryset=FoundGallery.objects.select_related('gallery').prefetch_related('gallery__archive_set')
                ),
                Prefetch(
                    'gallerymatch_set',
                    queryset=GalleryMatch.objects.select_related('gallery').prefetch_related('gallery__archive_set')
                ),
                'wanted_tags',
                'unwanted_tags',
                'categories',
                'mentions',
                'artists',
                'wanted_providers',
                'unwanted_providers',
            ).get(pk=pk)
        except WantedGallery.DoesNotExist:
            raise Http404("Wanted gallery does not exist")
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
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
        elif tool == "clear-possible-matches":
            wanted_gallery_instance.possible_matches.clear()
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
        elif tool == "create-matches-internally":
            wanted_gallery_instance.create_found_galleries_internally()
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
        elif tool == "select-as-match":
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
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
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
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
        elif tool == "stop-searching":
            wanted_gallery_instance.should_search = False
            # wanted_gallery_instance.keep_searching = False
            wanted_gallery_instance.save()
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
        elif tool == "toggle-public":
            wanted_gallery_instance.public_toggle()
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

        d = {
            "wanted_gallery": wanted_gallery_instance
        }
    return render(request, "viewer/wanted_gallery.html", d)
