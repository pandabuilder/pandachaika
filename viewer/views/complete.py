import re
from dal import autocomplete
from django import http
from django.db.models import Q

from viewer.models import Archive, Tag, Gallery, WantedGallery


class ArchiveAutocomplete(autocomplete.JalQuerySetView):
    model = Archive

    choice_html_format = u'''
        <a class="block choice" data-value="%s" href="%s">%s</a>
    '''
    empty_html_format = '<span class="block"><em>%s</em></span>'
    autocomplete_html_format = '%s'
    limit_choices = 10

    def choice_html(self, choice):
        return self.choice_html_format % (choice.title,
                                          choice.get_absolute_url(),
                                          choice.title)

    def render_to_response(self, context):

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return http.HttpResponse(self.autocomplete_html_format % html)

    def choices_for_request(self):
        qs = Archive.objects.all()

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

    def choice_html(self, choice):
        return self.choice_html_format % (choice.title,
                                          choice.get_absolute_url(),
                                          choice.title)

    def render_to_response(self, context):

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return http.HttpResponse(self.autocomplete_html_format % html)

    def choices_for_request(self):
        qs = Gallery.objects.eligible_for_use()

        q = self.request.GET.get('q', '')
        q_formatted = '%' + q.replace(' ', '%') + '%'
        m = re.search('(\d+)', q)
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

    def choice_html(self, choice):
        return self.choice_html_format % (choice.title,
                                          choice.get_absolute_url(),
                                          choice.title)

    def render_to_response(self, context):

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return http.HttpResponse(self.autocomplete_html_format % html)

    def choices_for_request(self):
        qs = WantedGallery.objects.all()

        q = self.request.GET.get('q', '')
        q_formatted = '%' + q.replace(' ', '%') + '%'
        q_object = Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted) | Q(search_title__ss=q_formatted) | Q(unwanted_title__ss=q_formatted)
        if self.request.user.is_authenticated:
            qs = qs.filter(
                q_object
            )
        else:
            qs = []

        return qs[0:self.limit_choices]


class SourceAutocomplete(autocomplete.JalQuerySetView):

    model = Archive

    choice_html_format = u'''
        <a class="block choice" data-value="%s">%s</a>
    '''
    empty_html_format = '<span class="block"><em>%s</em></span>'
    autocomplete_html_format = '%s'
    limit_choices = 10

    def render_to_response(self, context):

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return http.HttpResponse(self.autocomplete_html_format % html)

    def choice_html(self, choice):
        return self.choice_html_format % (self.get_result_value(choice),
                                          self.get_result_label(choice))

    @staticmethod
    def get_result_value(result):
        return str(result)

    @staticmethod
    def get_result_label(result):
        return str(result)

    def choices_for_request(self):

        q = self.request.GET.get('q', '')

        if self.request.user.is_authenticated:
            sources = Archive.objects.filter(source_type__contains=q).order_by('source_type').values_list('source_type', flat=True).distinct()
        else:
            sources = Archive.objects.filter(source_type__contains=q, public=True).order_by('source_type').values_list('source_type', flat=True).distinct()

        return sources[0:self.limit_choices]


class ReasonAutocomplete(autocomplete.JalQuerySetView):

    model = Archive

    choice_html_format = u'''
        <a class="block choice" data-value="%s">%s</a>
    '''
    empty_html_format = '<span class="block"><em>%s</em></span>'
    autocomplete_html_format = '%s'
    limit_choices = 10

    def render_to_response(self, context):

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return http.HttpResponse(self.autocomplete_html_format % html)

    def choice_html(self, choice):
        return self.choice_html_format % (self.get_result_value(choice),
                                          self.get_result_label(choice))

    @staticmethod
    def get_result_value(result):
        return str(result)

    @staticmethod
    def get_result_label(result):
        return str(result)

    def choices_for_request(self):

        q = self.request.GET.get('q', '')

        if self.request.user.is_authenticated:
            sources = Archive.objects.filter(reason__contains=q).order_by('reason').values_list('reason', flat=True).distinct()
        else:
            sources = Archive.objects.filter(reason__contains=q, public=True).order_by('reason').values_list('reason', flat=True).distinct()

        return sources[0:self.limit_choices]


class UploaderAutocomplete(autocomplete.JalQuerySetView):

    model = Archive

    choice_html_format = u'''
        <a class="block choice" data-value="%s">%s</a>
    '''
    empty_html_format = '<span class="block"><em>%s</em></span>'
    autocomplete_html_format = '%s'
    limit_choices = 10

    def render_to_response(self, context):

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return http.HttpResponse(self.autocomplete_html_format % html)

    def choice_html(self, choice):
        return self.choice_html_format % (self.get_result_value(choice),
                                          self.get_result_label(choice))

    @staticmethod
    def get_result_value(result):
        return str(result)

    @staticmethod
    def get_result_label(result):
        return str(result)

    def choices_for_request(self):

        q = self.request.GET.get('q', '')

        if self.request.user.is_authenticated:
            sources = Archive.objects.filter(gallery__uploader__icontains=q).order_by('gallery__uploader').values_list('gallery__uploader', flat=True).distinct()
        else:
            sources = Archive.objects.filter(gallery__uploader__icontains=q, public=True).order_by('gallery__uploader').values_list('gallery__uploader', flat=True).distinct()

        return sources[0:self.limit_choices]


class TagAutocomplete(autocomplete.JalQuerySetView):

    model = Tag

    choice_html_format = u'''
        <a class="block choice" data-value="%s">%s</a>
    '''
    empty_html_format = '<span class="block"><em>%s</em></span>'
    autocomplete_html_format = '%s'
    limit_choices = 10

    def render_to_response(self, context):

        html = ''.join(
            [self.choice_html(c) for c in self.choices_for_request()])

        if not html:
            html = self.empty_html_format % 'No matches found'

        return http.HttpResponse(self.autocomplete_html_format % html)

    def choice_html(self, choice):
        return self.choice_html_format % (self.get_result_value(choice),
                                          self.get_result_label(choice))

    def get_result_value(self, result):
        return self.modifier + str(result)

    def get_result_label(self, result):
        return self.modifier + str(result)

    def choices_for_request(self):

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
                Q(name__contains=tag_clean) |
                Q(scope__contains=tag_clean))

        return results.distinct()[0:self.limit_choices]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.modifier = ''


class NonCustomTagAutocomplete(autocomplete.Select2QuerySetView):
    model = Tag

    def get_result_value(self, result):
        return self.modifier + str(result)

    def get_result_label(self, result):
        return self.modifier + str(result)

    def get_queryset(self):

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
                Q(name__contains=tag_clean) |
                Q(scope__contains=tag_clean))

        return results.distinct()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.modifier = ''


class CustomTagAutocomplete(autocomplete.Select2QuerySetView):
    model = Tag

    def post(self, request):

        if not self.has_add_permission(request):
            return http.HttpResponseForbidden()

        t = request.POST.get('text', None).replace(" ", "_")

        if t is None:
            return http.HttpResponseBadRequest()

        scope_name = t.split(":", maxsplit=1)
        if len(scope_name) > 1:
            name = scope_name[1]
            scope = scope_name[0]
        else:
            name = t
            scope = ''
        custom_tag = Tag.objects.filter(name=name, scope=scope).first()
        if custom_tag:
            return http.JsonResponse({
                'id': custom_tag.pk,
                'text': str(custom_tag),
            })
        else:
            tag = Tag.objects.create(name=name, scope=scope, source='user')
            return http.JsonResponse({
                'id': tag.pk,
                'text': str(tag),
            })
