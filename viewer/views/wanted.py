import django.utils.timezone as django_tz
import logging
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseRedirect, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.conf import settings

from viewer.models import (
    Gallery, WantedGallery, FoundGallery, GalleryMatch
)
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
            'wanted_gallery': wanted_gallery_instance,
            'wanted_tag_lists': wanted_tag_lists,
            'unwanted_tag_lists': unwanted_tag_lists,
        }
    else:
        tool = request.GET.get('tool', '')
        tool_use_id = request.GET.get('tool-id', '')
        try:
            wanted_gallery_instance = WantedGallery.objects.get(pk=pk)
        except WantedGallery.DoesNotExist:
            raise Http404("Wanted gallery does not exist")
        if tool == 'search-providers-by-title':
            provider = request.GET.get('provider', '')
            try:
                cutoff = float(request.GET.get('cutoff', '0.4'))
            except ValueError:
                cutoff = 0.4
            try:
                max_matches = int(request.GET.get('max-matches', '10'))
            except ValueError:
                max_matches = 10
            matchers = crawler_settings.provider_context.get_matchers(
                crawler_settings, filter_name=provider, force=True, matcher_type='title'
            )
            for matcher_element in matchers:
                matcher = matcher_element[0]
                results = matcher.create_closer_matches_values(
                    wanted_gallery_instance.search_title,
                    cutoff=cutoff,
                    max_matches=max_matches
                )
                logger.info(
                    "Matcher: {} found {} results.".format(
                        str(matcher),
                        len(results)
                    )
                )
                for gallery_data in results:
                    gallery_data[1].dl_type = 'gallery_match'
                    gallery = Gallery.objects.update_or_create_from_values(gallery_data[1])
                    if gallery:
                        GalleryMatch.objects.get_or_create(
                            wanted_gallery=wanted_gallery_instance,
                            gallery=gallery,
                            defaults={'match_accuracy': gallery_data[2]})
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
        elif tool == 'search-internal-galleries-matches':
            provider = request.GET.get('provider', '')
            try:
                cutoff = float(request.GET.get('cutoff', '0.4'))
            except ValueError:
                cutoff = 0.4
            try:
                max_matches = int(request.GET.get('max-matches', '10'))
            except ValueError:
                max_matches = 10
            wanted_gallery_instance.search_gallery_title_internal_matches(
                provider_filter=provider,
                max_matches=max_matches,
                cutoff=cutoff
            )
            logger.info("Wanted gallery {} ({}) internal gallery match resulted in {} possible matches.".format(
                wanted_gallery_instance,
                reverse('viewer:wanted-gallery', args=(wanted_gallery_instance.pk,)),
                wanted_gallery_instance.possible_matches.count()
            ))
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
        elif tool == 'clear-possible-matches':
            wanted_gallery_instance.possible_matches.clear()
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
        elif tool == 'match-against-galleries':
            wanted_gallery_instance.match_against_galleries()
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
        elif tool == 'select-as-match':
            try:
                matched_gallery = Gallery.objects.get(pk=tool_use_id)
            except Gallery.DoesNotExist:
                raise Http404("Gallery does not exist")
            FoundGallery.objects.get_or_create(wanted_gallery=wanted_gallery_instance, gallery=matched_gallery)
            wanted_gallery_instance.found = True
            wanted_gallery_instance.date_found = django_tz.now()
            # wanted_gallery_instance.should_search = False
            # wanted_gallery_instance.keep_searching = False
            gm = GalleryMatch.objects.filter(wanted_gallery=wanted_gallery_instance, gallery=matched_gallery)
            if gm:
                gm.delete()
            wanted_gallery_instance.save()

            if wanted_gallery_instance.add_as_hidden and not matched_gallery.hidden:
                matched_gallery.hidden = True
                matched_gallery.save()

            logger.info("WantedGallery {} ({}) was matched with gallery {} ({}).".format(
                wanted_gallery_instance,
                reverse('viewer:wanted-gallery', args=(wanted_gallery_instance.pk,)),
                matched_gallery,
                reverse('viewer:gallery', args=(matched_gallery.pk,)),
            ))
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
        elif tool == 'remove-match':
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

            logger.info("Gallery {} ({}) was removed as a match for WantedGallery {} ({}).".format(
                matched_gallery,
                reverse('viewer:gallery', args=(matched_gallery.pk,)),
                wanted_gallery_instance,
                reverse('viewer:wanted-gallery', args=(wanted_gallery_instance.pk,))
            ))
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
        elif tool == 'stop-searching':
            wanted_gallery_instance.should_search = False
            # wanted_gallery_instance.keep_searching = False
            wanted_gallery_instance.save()
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
        elif tool == 'toggle-public':
            wanted_gallery_instance.public_toggle()
            return HttpResponseRedirect(request.META["HTTP_REFERER"])

        wanted_tag_lists = sort_tags(wanted_gallery_instance.wanted_tags.all())
        unwanted_tag_lists = sort_tags(wanted_gallery_instance.unwanted_tags.all())

        d = {
            'wanted_gallery': wanted_gallery_instance,
            'wanted_tag_lists': wanted_tag_lists,
            'unwanted_tag_lists': unwanted_tag_lists,
        }
    return render(request, "viewer/wanted_gallery.html", d)
