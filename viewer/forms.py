import os
from typing import Dict, Any, List

from django import forms
from django.forms.models import BaseModelFormSet, modelformset_factory, ModelChoiceField
from django.forms.utils import ErrorList
from django.utils.safestring import mark_safe
from django.utils.encoding import force_text
from django.forms.utils import flatatt


from dal import autocomplete
from dal_jal.widgets import JalWidgetMixin
from viewer.models import Archive, ArchiveMatches, Image, Gallery

from dal.widgets import (
    WidgetMixin
)

from django.utils.translation import ugettext_lazy


class JalTextWidget(JalWidgetMixin, WidgetMixin, forms.TextInput):

    class Media:
        css = {
            'all': (
                'autocomplete_light/vendor/jal/src/style.css',
            ),
        }
        js = (
            'autocomplete_light/jquery.init.js',
            'autocomplete_light/autocomplete.init.js',
            'autocomplete_light/vendor/jal/src/autocomplete.js',
            # 'autocomplete_light/vendor/jal/src/widget.js',
            'autocomplete_light/vendor/jal/src/text_widget.js',
            # 'autocomplete_light/forward.js',
            'autocomplete_light/jal.js',
        )

    def __init__(self, url=None, forward=None, attrs=None, *args,
                 **kwargs):
        forms.TextInput.__init__(self, *args, **kwargs)

        WidgetMixin.__init__(
            self,
            url=url,
            forward=forward
        )

        self.attrs.update(attrs)

    def render(self, name, value, attrs=None, renderer=None):
        """ Proxy Django's TextInput.render() """
        html = '''
            <input id="{id}" {attrs} type="text" name="{name}" value="{value}" />
        '''.format(
            id=attrs['id'],
            name=name,
            value=force_text('' if value is None else value),
            attrs=flatatt(self.build_attrs(attrs)),
        )

        return mark_safe(html)

    def build_attrs(self, *args: Any, **kwargs: Any) -> Dict[str, str]:

        attrs = {
            'data-autocomplete-choice-selector': '[data-value]',
            'data-widget-bootstrap': 'text',
            'data-autocomplete-url': self.url,
            'placeholder': ugettext_lazy('type some text to search in this autocomplete'),
        }

        attrs.update(self.attrs)

        if 'class' not in attrs.keys():
            attrs['class'] = ''
        attrs['class'] += ' autocomplete-light-text-widget autocomplete vTextField'

        return attrs


class MatchesModelChoiceField(ModelChoiceField):
    def label_from_instance(self, obj: Gallery) -> str:
        first_artist_tag = obj.tags.first_artist_tag()
        tag_name = ''
        if first_artist_tag:
            tag_name = obj.tags.first_artist_tag().name
        return "{} >> {} >> {} >> {} >> {} >> {}".format(tag_name, obj.pk, obj.title, obj.provider, obj.filecount, obj.filesize)


class ArchiveSearchForm(forms.Form):

    title = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url='archive-autocomplete',
            attrs={
                'class': 'form-control',
                'aria-label': 'title',
                'placeholder': 'Title or Japanese title, mouse click on autocomplete opens it',
                'data-autocomplete-minimum-characters': 3,
                'data-autocomplete-xhr-wait': 50,
                'data-autocomplete-auto-hilight-first': 0,
                'data-autocomplete-bind-mouse-down': 0,
            },
        ),
    )

    tags = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url='tag-autocomplete',
            attrs={
                'class': 'form-control',
                'aria-label': 'tags',
                'placeholder': 'Comma separated tags. - to exclude, ^ for exact matching',
                'data-autocomplete-minimum-characters': 3,
            },
        ),
    )


class ArchiveSearchSimpleForm(forms.Form):

    filecount_from = forms.IntegerField(
        required=False,
        label='Images',
        widget=forms.NumberInput(attrs={'class': 'form-control number-input', 'placeholder': 'from'})
    )
    filecount_to = forms.IntegerField(
        required=False,
        label='',
        widget=forms.NumberInput(attrs={'class': 'form-control number-input', 'placeholder': 'to'})
    )
    # Search by rating: is it really needed?
    # rating_from = forms.IntegerField(
    #     required=False,
    #     label='Rating',
    #     min_value=0,
    #     widget=forms.NumberInput(attrs={'class': 'form-control number-input', 'placeholder': 'from', 'size': 3})
    # )
    # rating_to = forms.IntegerField(
    #     required=False,
    #     label='',
    #     min_value=0,
    #     widget=forms.NumberInput(attrs={'class': 'form-control number-input', 'placeholder': 'to', 'size': 3})
    # )
    posted_from = forms.DateTimeField(
        required=False,
        label='Posted',
        widget=forms.DateInput(attrs={'class': 'form-control', 'placeholder': 'from', 'type': 'date', 'size': 9}),
        input_formats=['%Y-%m-%d']
    )
    posted_to = forms.DateTimeField(
        required=False,
        label='',
        widget=forms.DateInput(attrs={'class': 'form-control', 'placeholder': 'to', 'type': 'date', 'size': 9}),
        input_formats=['%Y-%m-%d']
    )
    source_type = forms.CharField(
        required=False,
        label='Source',
        widget=JalTextWidget(
            url='source-autocomplete',
            attrs={
                'class': 'form-control',
                'placeholder': 'panda, fakku',
                'data-autocomplete-minimum-characters': 3,
                'size': 10,
            },
        ),
    )

    reason = forms.CharField(
        required=False,
        label='Reason',
        widget=JalTextWidget(
            url='reason-autocomplete',
            attrs={
                'class': 'form-control',
                'placeholder': 'wani, etc.',
                'data-autocomplete-minimum-characters': 3,
                'size': 10,
            },
        ),
    )

    uploader = forms.CharField(
        required=False,
        label='Uploader',
        widget=JalTextWidget(
            url='uploader-autocomplete',
            attrs={
                'class': 'form-control',
                'placeholder': '',
                'data-autocomplete-minimum-characters': 3,
                'size': 10,
            },
        ),
    )

    category = forms.CharField(
        required=False,
        label='Category',
        widget=JalTextWidget(
            url='category-autocomplete',
            attrs={
                'class': 'form-control',
                'placeholder': '',
                'data-autocomplete-minimum-characters': 3,
                'size': 10,
            },
        ),
    )


class SpanErrorList(ErrorList):
    def __str__(self) -> str:              # __unicode__ on Python 2
        return self.as_divs()

    def as_divs(self) -> str:
        if not self:
            return ''
        return '<div class="errorlist">%s</div>' % ''.join(['<span  class="alert alert-danger error" role="alert">%s</span>' % e for e in self])


class ArchiveModForm(forms.ModelForm):

    class Meta:
        model = Archive
        fields = ['title', 'title_jpn', 'source_type', 'reason', 'possible_matches', 'custom_tags', 'zipped']
        widgets = {
            'custom_tags': autocomplete.ModelSelect2Multiple(
                url='customtag-autocomplete',
                # widget_attrs={'data-widget-bootstrap': 'customtag-widget', },
                attrs={'size': 1, 'data-placeholder': 'Custom tag name', 'class': 'form-control'}),
            'title': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'title_jpn': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'source_type': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'reason': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'zipped': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            # 'possible_matches': forms.widgets.Select(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # self.archive = kwargs.pop("archive")
        super(ArchiveModForm, self).__init__(*args, **kwargs)
        self.fields["possible_matches"].queryset = self.instance.possible_matches.order_by(
            "-archivematches__match_accuracy")
#     custom_tags = forms.ModelMultipleChoiceField(required=False,
#                                                  queryset=CustomTag.objects.all(),
#                                                  widget=autocomplete_light.MultipleChoiceWidget('CustomTagAutocomplete'))

    possible_matches = MatchesModelChoiceField(
        required=False,
        queryset=ArchiveMatches.objects.none(),
        widget=forms.widgets.Select(attrs={'class': 'form-control'}),
    )


class GallerySearchForm(forms.Form):

    title = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url='gallery-autocomplete',
            attrs={
                'class': 'form-control',
                'placeholder': 'Title, Japanese title or ex gallery ID, mouse click on autocomplete opens it',
                'data-autocomplete-minimum-characters': 3,
                'data-autocomplete-xhr-wait': 50,
                'data-autocomplete-auto-hilight-first': 0,
                'data-autocomplete-bind-mouse-down': 0,
            },
        ),
    )

    tags = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url='tag-autocomplete',
            attrs={
                'class': 'form-control',
                'placeholder': 'Comma separated tags. - to exclude, ^ for exact matching',
                'data-autocomplete-minimum-characters': 3,
            },
        ),
    )


class WantedGallerySearchForm(forms.Form):

    title = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url='wanted-gallery-autocomplete',
            attrs={
                'class': 'form-control',
                'placeholder': 'Title, Japanese title or search title, mouse click on autocomplete opens it',
                'data-autocomplete-minimum-characters': 3,
                'data-autocomplete-xhr-wait': 50,
                'data-autocomplete-auto-hilight-first': 0,
                'data-autocomplete-bind-mouse-down': 0,
            },
        ),
    )

    tags = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url='tag-autocomplete',
            attrs={
                'class': 'form-control',
                'placeholder': 'Comma separated tags. - to exclude, ^ for exact matching',
                'data-autocomplete-minimum-characters': 3,
            },
        ),
    )


class ImageForm(forms.ModelForm):
    position = forms.IntegerField(required=True,
                                  widget=forms.NumberInput(attrs={'size': 10}))

    class Meta:
        model = Image
        fields = ['position']

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(ImageForm, self).__init__(*args, **kwargs)
#         self.fields["position"].choices = [
#             (x, x) for x in Image.objects.filter(
#                 archive=self.instance.archive
#             ).values_list('position', flat=True)
#         ]
        if self.instance.image:
            self.fields["position"].label = os.path.basename(self.instance.image.path)
        else:
            self.fields["position"].label = self.instance.position


class BaseImageFormSet(BaseModelFormSet):
    def clean(self) -> None:
        if any(self.errors):
            return
        positions: List[int] = []
        for form in self.forms:
            position = form.cleaned_data['position']
            if position in positions:
                raise forms.ValidationError(
                    "Images positions must be unique: " +
                    str(position)
                )
            positions.append(position)


ImageFormSet = modelformset_factory(
    Image, form=ImageForm, extra=0,
    formset=BaseImageFormSet,
    can_delete=True)
