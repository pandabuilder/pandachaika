import json
import re
from collections.abc import Iterable
from typing import Any, Optional

import typing
from dal import autocomplete
from dal.views import BaseQuerySetView
from dal_select2.views import Select2ViewMixin
from django import http
from django.db.models import Q, QuerySet
from django.http import HttpResponse, HttpRequest
from django.template import Context
from django.utils.html import format_html
from django.conf import settings

from viewer.models import Archive, Tag, Gallery, WantedGallery, ArchiveGroup, Provider, ArchiveManageEntry

if typing.TYPE_CHECKING:
    from django_stubs_ext import ValuesQuerySet


crawler_settings = settings.CRAWLER_SETTINGS


class ArchiveAutocomplete(autocomplete.JalQuerySetView):
    model = Archive

    choice_html_format = u'''
        <a class="block choice" data-value="%s" href="%s">%s</a>
    '''
    empty_html_format = '<span class="block"><em>%s</em></span>'
    autocomplete_html_format = '%s'
    limit_choices = 10

    def choice_html(self, choice: Archive) -> str:
        return self.choice_html_format % (choice.title,
                                          choice.get_absolute_url(),
                                          choice.title)

    def render_to_response(self, context: Context) -> HttpResponse:

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return HttpResponse(self.autocomplete_html_format % html)

    def choices_for_request(self) -> Iterable[Archive]:
        qs = Archive.objects.all().order_by('pk')

        q = self.request.GET.get('q', '')
        q_formatted = '%' + q.replace(' ', '%') + '%'
        if self.request.user.is_authenticated:
            qs = qs.filter(
                Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
            )
        else:
            qs = qs.filter(public=True).order_by('-public_date').filter(
                Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
            )

        return qs[0:self.limit_choices]


class GalleryAutocomplete(autocomplete.JalQuerySetView):

    model = Gallery

    choice_html_format = u'''
        <a class="block choice" data-value="%s" href="%s">%s</a>
    '''
    empty_html_format = '<span class="block"><em>%s</em></span>'
    autocomplete_html_format = '%s'
    limit_choices = 10

    def choice_html(self, choice: Gallery) -> str:
        return self.choice_html_format % (choice.title,
                                          choice.get_absolute_url(),
                                          choice.title)

    def render_to_response(self, context: Context) -> HttpResponse:

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return HttpResponse(self.autocomplete_html_format % html)

    def choices_for_request(self) -> Iterable[Gallery]:
        qs = Gallery.objects.eligible_for_use().order_by('pk')

        q = self.request.GET.get('q', '')
        q_formatted = '%' + q.replace(' ', '%') + '%'
        m = re.search(r'(\d+)', q)
        if m:
            q_object = Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted) | Q(gid__exact=m.group(1))
        else:
            q_object = Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
        if self.request.user.is_authenticated:
            qs = qs.filter(
                q_object
            )
        else:
            qs = qs.filter(public=True).filter(
                q_object
            )

        return qs[0:self.limit_choices]


class GalleryAllAutocomplete(GalleryAutocomplete):

    def choices_for_request(self) -> Iterable[Gallery]:
        qs = Gallery.objects.all().order_by('pk')

        q = self.request.GET.get('q', '')
        q_formatted = '%' + q.replace(' ', '%') + '%'
        m = re.search(r'(\d+)', q)
        if m:
            q_object = Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted) | Q(gid__exact=m.group(1))
        else:
            q_object = Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
        if self.request.user.is_authenticated:
            qs = qs.filter(
                q_object
            )
        else:
            qs = qs.filter(public=True).filter(
                q_object
            )

        return qs[0:self.limit_choices]


class WantedGalleryAutocomplete(autocomplete.JalQuerySetView):

    model = WantedGallery

    choice_html_format = u'''
        <a class="block choice" data-value="%s" href="%s">%s</a>
    '''
    empty_html_format = '<span class="block"><em>%s</em></span>'
    autocomplete_html_format = '%s'
    limit_choices = 10

    def choice_html(self, choice: WantedGallery) -> str:
        return self.choice_html_format % (choice.title,
                                          choice.get_absolute_url(),
                                          choice.title)

    def render_to_response(self, context: Context) -> HttpResponse:

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return HttpResponse(self.autocomplete_html_format % html)

    def choices_for_request(self) -> Iterable[WantedGallery]:
        qs = WantedGallery.objects.all().order_by('pk')

        q = self.request.GET.get('q', '')
        q_formatted = '%' + q.replace(' ', '%') + '%'
        q_object = Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted) | Q(search_title__ss=q_formatted) | Q(unwanted_title__ss=q_formatted)
        if self.request.user.is_authenticated:
            qs = qs.filter(
                q_object
            )
        else:
            return []

        return qs[0:self.limit_choices]


class WantedGalleryColAutocomplete(WantedGalleryAutocomplete):
    def choice_html(self, choice: WantedGallery) -> str:
        return self.choice_html_format % (choice.title,
                                          choice.get_col_absolute_url(),
                                          choice.title)


class ArchiveGroupAutocomplete(autocomplete.JalQuerySetView):
    model = ArchiveGroup

    choice_html_format = u'''
        <a class="block choice" data-value="%s" href="%s">%s</a>
    '''
    empty_html_format = '<span class="block"><em>%s</em></span>'
    autocomplete_html_format = '%s'
    limit_choices = 10

    def choice_html(self, choice: ArchiveGroup) -> str:
        return self.choice_html_format % (choice.title,
                                          choice.get_absolute_url(),
                                          choice.title)

    def render_to_response(self, context: Context) -> HttpResponse:

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return HttpResponse(self.autocomplete_html_format % html)

    def choices_for_request(self) -> Iterable[ArchiveGroup]:
        qs = ArchiveGroup.objects.all().order_by('position')

        q = self.request.GET.get('q', '')
        q_formatted = '%' + q.replace(' ', '%') + '%'
        if self.request.user.is_authenticated:
            qs = qs.filter(
                Q(title__ss=q_formatted)
            )
        else:
            qs = qs.filter(public=True).filter(
                Q(title__ss=q_formatted)
            )

        return qs[0:self.limit_choices]


class ArchiveManageEntryFieldAutocomplete(autocomplete.JalQuerySetView):
    model = ArchiveManageEntry

    choice_html_format = u'''
        <a class="block choice" data-value="%s">%s</a>
    '''
    empty_html_format = '<span class="block"><em>%s</em></span>'
    autocomplete_html_format = '%s'
    limit_choices = 10

    def render_to_response(self, context: Context) -> HttpResponse:

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return HttpResponse(self.autocomplete_html_format % html)

    def choice_html(self, choice: Optional[str]) -> str:
        return self.choice_html_format % (self.get_result_value(choice),
                                          self.get_result_label(choice))

    @staticmethod
    def get_result_value(result: Optional[str]) -> str:
        if result:
            return result
        else:
            return ''

    @staticmethod
    def get_result_label(result: Optional[str]) -> str:
        if result:
            return result
        else:
            return ''

    def choices_for_request(self) -> 'Iterable[Optional[str]]':
        raise NotImplementedError


class ArchiveManageEntryMarkReasonAutocomplete(ArchiveManageEntryFieldAutocomplete):
    def choices_for_request(self) -> 'Iterable[Optional[str]]':

        q = self.request.GET.get('q', '')

        if self.request.user.is_authenticated:
            mark_reasons = ArchiveManageEntry.objects.filter(mark_reason__contains=q).order_by('mark_reason').values_list('mark_reason', flat=True).distinct()
            return mark_reasons[0:self.limit_choices]
        return []


class ArchiveFieldAutocomplete(autocomplete.JalQuerySetView):

    model = Archive

    choice_html_format = u'''
        <a class="block choice" data-value="%s">%s</a>
    '''
    empty_html_format = '<span class="block"><em>%s</em></span>'
    autocomplete_html_format = '%s'
    limit_choices = 10

    def render_to_response(self, context: Context) -> HttpResponse:

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return HttpResponse(self.autocomplete_html_format % html)

    def choice_html(self, choice: Optional[str]) -> str:
        return self.choice_html_format % (self.get_result_value(choice),
                                          self.get_result_label(choice))

    @staticmethod
    def get_result_value(result: Optional[str]) -> str:
        if result:
            return result
        else:
            return ''

    @staticmethod
    def get_result_label(result: Optional[str]) -> str:
        if result:
            return result
        else:
            return ''

    def choices_for_request(self) -> 'ValuesQuerySet[Archive, Optional[str]]':
        raise NotImplementedError


class GalleryFieldAutocomplete(autocomplete.JalQuerySetView):

    model = Gallery

    choice_html_format = u'''
        <a class="block choice" data-value="%s">%s</a>
    '''
    empty_html_format = '<span class="block"><em>%s</em></span>'
    autocomplete_html_format = '%s'
    limit_choices = 10

    def render_to_response(self, context: Context) -> HttpResponse:

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return HttpResponse(self.autocomplete_html_format % html)

    def choice_html(self, choice: Optional[str]) -> str:
        return self.choice_html_format % (self.get_result_value(choice),
                                          self.get_result_label(choice))

    @staticmethod
    def get_result_value(result: Optional[str]) -> str:
        if result:
            return result
        else:
            return ''

    @staticmethod
    def get_result_label(result: Optional[str]) -> str:
        if result:
            return result
        else:
            return ''

    def choices_for_request(self) -> 'ValuesQuerySet[Gallery, Optional[str]]':
        raise NotImplementedError


class SourceAutocomplete(ArchiveFieldAutocomplete):

    def choices_for_request(self) -> 'ValuesQuerySet[Archive, Optional[str]]':

        q = self.request.GET.get('q', '')

        if self.request.user.is_authenticated:
            sources = Archive.objects.filter(source_type__contains=q).order_by('source_type').values_list('source_type', flat=True).distinct()
        else:
            sources = Archive.objects.filter(source_type__contains=q, public=True).order_by('source_type').values_list('source_type', flat=True).distinct()

        return sources[0:self.limit_choices]


class ProviderAutocomplete(ArchiveFieldAutocomplete):

    def choices_for_request(self) -> 'ValuesQuerySet[Archive, Optional[str]]':

        q = self.request.GET.get('q', '')

        if self.request.user.is_authenticated:
            sources = Archive.objects.filter(gallery__provider__icontains=q).order_by('gallery__provider').values_list('gallery__provider', flat=True).distinct()
        else:
            sources = Archive.objects.filter(gallery__provider__icontains=q, public=True).order_by('gallery__provider').values_list('gallery__provider', flat=True).distinct()

        return sources[0:self.limit_choices]


class ReasonAutocomplete(ArchiveFieldAutocomplete):

    def choices_for_request(self) -> 'ValuesQuerySet[Archive, Optional[str]]':

        q = self.request.GET.get('q', '')

        if self.request.user.is_authenticated:
            sources = Archive.objects.filter(reason__contains=q).order_by('reason').values_list('reason', flat=True).distinct()
        else:
            sources = Archive.objects.filter(reason__contains=q, public=True).order_by('reason').values_list('reason', flat=True).distinct()

        return sources[0:self.limit_choices]


class UploaderAutocomplete(ArchiveFieldAutocomplete):

    def choices_for_request(self) -> 'ValuesQuerySet[Archive, Optional[str]]':

        q = self.request.GET.get('q', '')

        if self.request.user.is_authenticated:
            sources = Archive.objects.filter(gallery__uploader__icontains=q).order_by('gallery__uploader').values_list('gallery__uploader', flat=True).distinct()
        else:
            sources = Archive.objects.filter(gallery__uploader__icontains=q, public=True).order_by('gallery__uploader').values_list('gallery__uploader', flat=True).distinct()

        return sources[0:self.limit_choices]


class CategoryAutocomplete(ArchiveFieldAutocomplete):

    def choices_for_request(self) -> 'ValuesQuerySet[Archive, Optional[str]]':

        q = self.request.GET.get('q', '')

        if self.request.user.is_authenticated:
            sources = Archive.objects.filter(gallery__category__icontains=q).order_by('gallery__category').values_list('gallery__category', flat=True).distinct()
        else:
            sources = Archive.objects.filter(gallery__category__icontains=q, public=True).order_by('gallery__category').values_list('gallery__category', flat=True).distinct()

        return sources[0:self.limit_choices]


# Gallery-based autocompletes
class GalleryProviderAutocomplete(GalleryFieldAutocomplete):

    def choices_for_request(self) -> 'ValuesQuerySet[Gallery, Optional[str]]':

        q = self.request.GET.get('q', '')

        if self.request.user.is_authenticated:
            providers = Gallery.objects.filter(provider__icontains=q).order_by('provider').distinct().values_list('provider', flat=True)
        else:
            providers = Gallery.objects.filter(provider__icontains=q, public=True).order_by('provider').distinct().values_list('provider', flat=True)

        return providers[0:self.limit_choices]


class GalleryCategoryAutocomplete(GalleryFieldAutocomplete):

    def choices_for_request(self) -> 'ValuesQuerySet[Gallery, Optional[str]]':

        q = self.request.GET.get('q', '')

        if self.request.user.is_authenticated:
            categories = Gallery.objects.filter(category__icontains=q).order_by('category').distinct().values_list('category', flat=True)
        else:
            categories = Gallery.objects.filter(category__icontains=q, public=True).order_by('category').distinct().values_list('category', flat=True)

        return categories[0:self.limit_choices]


class GalleryUploaderAutocomplete(GalleryFieldAutocomplete):

    def choices_for_request(self) -> 'ValuesQuerySet[Gallery, Optional[str]]':

        q = self.request.GET.get('q', '')

        if self.request.user.is_authenticated:
            uploaders = Gallery.objects.filter(uploader__icontains=q).order_by('uploader').distinct().values_list('uploader', flat=True)
        else:
            uploaders = Gallery.objects.filter(uploader__icontains=q, public=True).order_by('uploader').distinct().values_list('uploader', flat=True)

        return uploaders[0:self.limit_choices]


class GalleryReasonAutocomplete(GalleryFieldAutocomplete):

    def choices_for_request(self) -> 'ValuesQuerySet[Gallery, Optional[str]]':

        q = self.request.GET.get('q', '')

        if self.request.user.is_authenticated:
            reasons = Gallery.objects.filter(reason__icontains=q).order_by('reason').distinct().values_list('reason', flat=True)
        else:
            reasons = Gallery.objects.filter(reason__icontains=q, public=True).order_by('reason').distinct().values_list('reason', flat=True)

        return reasons[0:self.limit_choices]


class TagAutocomplete(autocomplete.JalQuerySetView):

    model = Tag

    choice_html_format = u'''
        <a class="block choice" data-value="%s">%s</a>
    '''
    empty_html_format = '<span class="block"><em>%s</em></span>'
    autocomplete_html_format = '%s'
    limit_choices = 10

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.modifier = ''

    def render_to_response(self, context: Context) -> HttpResponse:

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return HttpResponse(self.autocomplete_html_format % html)

    def choice_html(self, choice: Tag) -> str:
        return self.choice_html_format % (self.get_result_value(choice),
                                          self.get_result_label(choice))

    def get_result_value(self, result: Tag) -> str:
        return self.modifier + str(result)

    def get_result_label(self, result: Tag) -> str:
        return self.modifier + str(result)

    def choices_for_request(self) -> Iterable[Tag]:

        tag_clean = self.request.GET.get('q', '').replace(" ", "_")
        m = re.match(r"^([-^])", tag_clean)
        if m:
            self.modifier = m.group(1)
            tag_clean = tag_clean.replace(self.modifier, "")
        else:
            self.modifier = ''
        scope_name = tag_clean.split(":", maxsplit=1)

        if len(scope_name) > 1:
            results = Tag.objects.filter(
                Q(name__contains=scope_name[1]),
                Q(scope__contains=scope_name[0]))
        else:
            results = Tag.objects.filter(
                Q(name__contains=tag_clean)
                | Q(scope__contains=tag_clean))

        return results.order_by('pk')[0:self.limit_choices]


class Select2ViewMixinNoCreate(Select2ViewMixin):
    def render_to_response(self, context):
        """Return a JSON response in Select2 format."""
        return http.HttpResponse(
            json.dumps({
                'results': self.get_results(context),
                'pagination': {
                    'more': self.has_more(context)
                }
            }),
            content_type='application/json',
        )


class Select2QuerySetViewNoCreate(Select2ViewMixinNoCreate, BaseQuerySetView):
    """List options for a Select2 widget."""


class TagPkAutocomplete(Select2QuerySetViewNoCreate):
    model = Tag


class ProviderPkAutocomplete(Select2QuerySetViewNoCreate):
    model = Provider


class NonCustomTagAutocomplete(autocomplete.Select2QuerySetView):
    model = Tag

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.modifier = ''

    def get_result_value(self, result: Tag) -> str:
        return self.modifier + str(result)

    def get_result_label(self, result: Tag) -> str:
        return self.modifier + str(result)

    def get_queryset(self) -> QuerySet:

        tag_clean = self.request.GET.get('q', '').replace(" ", "_")
        m = re.match(r"^([-^])", tag_clean)
        if m:
            self.modifier = m.group(1)
            tag_clean = tag_clean.replace(self.modifier, "")
        else:
            self.modifier = ''
        scope_name = tag_clean.split(":", maxsplit=1)

        if len(scope_name) > 1:
            results = Tag.objects.exclude(source='user').filter(
                Q(name__contains=scope_name[1]),
                Q(scope__contains=scope_name[0]))
        else:
            results = Tag.objects.exclude(source='user').filter(
                Q(name__contains=tag_clean)
                | Q(scope__contains=tag_clean))

        return results.order_by('pk')


class CustomTagAutocomplete(autocomplete.Select2QuerySetView):
    model = Tag

    def post(self, request: HttpRequest) -> HttpResponse:

        if not self.has_add_permission(request):
            return http.HttpResponseForbidden()

        t = request.POST.get('text', None)

        if t is None:
            return http.HttpResponseBadRequest()

        t = t.replace(" ", "_")

        scope_name = t.split(":", maxsplit=1)
        if len(scope_name) > 1:
            name = scope_name[1]
            scope = scope_name[0]
        else:
            name = t
            scope = ''
        custom_tag = Tag.objects.get_or_create(name=name, scope=scope, defaults={'source': 'user'})[0]
        return http.JsonResponse({
            'id': custom_tag.pk,
            'text': str(custom_tag),
        })

    def get_queryset(self) -> QuerySet:

        tag_clean = self.request.GET.get('q', '').replace(" ", "_")

        scope_name = tag_clean.split(":", maxsplit=1)

        if len(scope_name) > 1:
            results = Tag.objects.filter(
                Q(name__contains=scope_name[1]),
                Q(scope__contains=scope_name[0]))
        else:
            results = Tag.objects.filter(
                Q(name__contains=tag_clean)
                | Q(scope__contains=tag_clean))

        return results.order_by('source')


class GallerySelectAutocomplete(autocomplete.Select2QuerySetView):
    model = Gallery
    limit_choices = 10

    def get_result_label(self, result: Gallery) -> str:
        return "({}) ({}) {}".format(result.pk, result.title, result.provider)

    def get_queryset(self) -> QuerySet:
        qs = Gallery.objects.eligible_for_use().order_by('pk')

        q = self.q
        q_formatted = '%' + q.replace(' ', '%') + '%'

        gallery_id_provider = None

        parsers = crawler_settings.provider_context.get_parsers(crawler_settings)

        for parser in parsers:
            if parser.id_from_url_implemented():
                accepted_urls = parser.filter_accepted_urls((q,))
                if accepted_urls:
                    gallery_id_provider = parser.id_from_url(accepted_urls[0]), parser.name
                    break

        if gallery_id_provider:
            q_object = Q(gid=gallery_id_provider[0], provider=gallery_id_provider[1])
        else:
            q_object = Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)
        if self.request.user.is_authenticated:
            qs = qs.filter(
                q_object
            )
        else:
            qs = qs.filter(public=True).filter(
                q_object
            )

        return qs[0:self.limit_choices]


class ArchiveSelectSimpleAutocomplete(autocomplete.Select2QuerySetView):
    model = Archive
    limit_choices = 10

    def get_result_label(self, result: Archive) -> str:
        return "({}) ({}) {}".format(result.pk, result.title, result.source_type)

    def get_queryset(self) -> QuerySet:
        qs = Archive.objects.all().order_by('pk')

        q = self.q
        q_formatted = '%' + q.replace(' ', '%') + '%'

        q_object = Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)

        if self.request.user.is_authenticated:
            qs = qs.filter(
                q_object
            )
        else:
            qs = qs.filter(public=True).filter(
                q_object
            )

        return qs[0:self.limit_choices]


class ArchiveSelectAutocomplete(autocomplete.Select2QuerySetView):
    model = Archive
    limit_choices = 10

    def get_result_label(self, result: Archive) -> str:
        return format_html('<div class="archive-complete-container"><div class="archive-complete-title">{}</div><img src="{}"></div>', result.title, result.thumbnail.url)

    def get_queryset(self) -> QuerySet:
        qs = Archive.objects.all().order_by('pk')

        q = self.q
        q_formatted = '%' + q.replace(' ', '%') + '%'

        q_object = Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted)

        if self.request.user.is_authenticated:
            qs = qs.filter(
                q_object
            )
        else:
            qs = qs.filter(public=True).filter(
                q_object
            )

        return qs[0:self.limit_choices]


class ArchiveGroupSelectAutocomplete(autocomplete.Select2QuerySetView):
    model = ArchiveGroup
    limit_choices = 10

    def get_result_label(self, result: ArchiveGroup) -> str:
        return "({}) ({})".format(result.pk, result.title)

    def get_queryset(self) -> QuerySet:
        qs = ArchiveGroup.objects.all().order_by('position')

        q = self.q
        q_formatted = '%' + q.replace(' ', '%') + '%'
        if self.request.user.is_authenticated:
            qs = qs.filter(
                Q(title__ss=q_formatted)
            )
        else:
            qs = qs.filter(public=True).filter(
                Q(title__ss=q_formatted)
            )

        return qs[0:self.limit_choices]
