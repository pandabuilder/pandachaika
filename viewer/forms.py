import os
from typing import Dict, Any, List

from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import TemporaryUploadedFile
from django.forms.models import BaseModelFormSet, modelformset_factory, ModelChoiceField, ModelForm, \
    inlineformset_factory, BaseInlineFormSet
from django.forms.utils import ErrorList
from django.utils.safestring import mark_safe
from django.utils.encoding import force_str
from django.forms.utils import flatatt
from django.conf import settings


from dal import autocomplete
from dal_jal.widgets import JalWidgetMixin
from viewer.models import Archive, ArchiveMatches, Image, Gallery, Profile, WantedGallery, ArchiveGroup, \
    ArchiveGroupEntry, Tag

from dal.widgets import (
    WidgetMixin
)

from django.utils.translation import gettext_lazy

crawler_settings = settings.CRAWLER_SETTINGS


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
            value=force_str('' if value is None else value),
            attrs=flatatt(self.build_attrs(attrs)),
        )

        return mark_safe(html)

    def build_attrs(self, *args: Any, **kwargs: Any):

        attrs = {
            'data-autocomplete-choice-selector': '[data-value]',
            'data-widget-bootstrap': 'text',
            'data-autocomplete-url': self.url,
            'placeholder': gettext_lazy('type some text to search in this autocomplete'),
        }

        attrs.update(self.attrs)

        if 'class' not in attrs.keys():
            attrs['class'] = ''
        attrs['class'] += ' autocomplete-light-text-widget autocomplete vTextField'

        return attrs


class MatchesModelChoiceField(ModelChoiceField):
    def label_from_instance(self, obj: Gallery) -> str:  # type: ignore[override]
        first_artist_tag = Tag.objects.filter(gallery=obj.id).first_artist_tag()  # type: ignore
        tag_name = ''
        if first_artist_tag:
            tag_name = first_artist_tag.name
        return "{} >> {} >> {} >> {} >> {} >> {}".format(tag_name, obj.pk, obj.title, obj.provider, obj.filecount, obj.filesize)


class ArchiveGroupSearchForm(forms.Form):

    title = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url='archive-group-autocomplete',
            attrs={
                'class': 'form-control',
                'aria-label': 'title',
                'placeholder': 'Title, mouse click on autocomplete opens it',
                'data-autocomplete-minimum-characters': 3,
                'data-autocomplete-xhr-wait': 50,
                'data-autocomplete-auto-hilight-first': 0,
                'data-autocomplete-bind-mouse-down': 0,
            },
        ),
    )


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
                'placeholder': 'Comma separated tags. - to exclude, ^ for exact matching.'
                               ' End a term with : to match scope only',
                'data-autocomplete-minimum-characters': 3,
            },
        ),
    )


class ArchiveSearchSimpleForm(forms.Form):

    filecount_from = forms.IntegerField(
        required=False,
        label='Images',
        widget=forms.NumberInput(attrs={'class': 'form-control number-input mr-sm-1', 'placeholder': 'from'})
    )
    filecount_to = forms.IntegerField(
        required=False,
        label='',
        widget=forms.NumberInput(attrs={'class': 'form-control number-input mr-sm-1', 'placeholder': 'to'})
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
        widget=forms.DateInput(attrs={'class': 'form-control mr-sm-1', 'placeholder': 'from', 'type': 'date', 'size': 9}),
        input_formats=['%Y-%m-%d']
    )
    posted_to = forms.DateTimeField(
        required=False,
        label='',
        widget=forms.DateInput(attrs={'class': 'form-control mr-sm-1', 'placeholder': 'to', 'type': 'date', 'size': 9}),
        input_formats=['%Y-%m-%d']
    )
    source_type = forms.CharField(
        required=False,
        label='Source',
        widget=JalTextWidget(
            url='source-autocomplete',
            attrs={
                'class': 'form-control mr-sm-1',
                'placeholder': 'panda, etc.',
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
                'class': 'form-control mr-sm-1',
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
                'class': 'form-control mr-sm-1',
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

    filesize_from = forms.IntegerField(
        required=False,
        label='Size',
        widget=forms.NumberInput(attrs={'class': 'form-control mr-sm-1', 'placeholder': 'from'})
    )
    filesize_to = forms.IntegerField(
        required=False,
        label='',
        widget=forms.NumberInput(attrs={'class': 'form-control mr-sm-1', 'placeholder': 'to'})
    )


class SpanErrorList(ErrorList):
    def __str__(self) -> str:              # __unicode__ on Python 2
        return self.as_divs()

    def as_divs(self) -> str:
        if not self:
            return ''
        return '<div class="errorlist">%s</div>' % ''.join(['<div  class="alert alert-danger error" role="alert">%s</div>' % e for e in self])


class ArchiveGroupSelectForm(forms.Form):

    archive_group = forms.ModelMultipleChoiceField(
        required=False,
        queryset=ArchiveGroup.objects.none(),
        widget=autocomplete.ModelSelect2Multiple(
            url='archive-group-select-autocomplete',
            attrs={'size': 1, 'data-placeholder': 'Group name', 'class': 'form-control'}),
    )


class ArchiveModForm(forms.ModelForm):

    class Meta:
        model = Archive
        fields = ['title', 'title_jpn', 'source_type', 'reason', 'possible_matches', 'custom_tags', 'zipped',
                  'alternative_sources', 'details']
        widgets = {
            'custom_tags': autocomplete.ModelSelect2Multiple(
                url='customtag-autocomplete',
                attrs={'size': 1, 'data-placeholder': 'Custom tag name', 'class': 'form-control'}),
            'alternative_sources': autocomplete.ModelSelect2Multiple(
                url='gallery-select-autocomplete',
                attrs={'size': 1, 'data-placeholder': 'Alternative source', 'class': 'form-control'}),
            'title': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'title_jpn': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'source_type': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'reason': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'zipped': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'details': forms.widgets.Textarea(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(ArchiveModForm, self).__init__(*args, **kwargs)
        self.fields["possible_matches"].queryset = self.instance.possible_matches.order_by(
            "-archivematches__match_accuracy")

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
                'placeholder': 'Comma separated tags. - to exclude, ^ for exact matching.'
                               ' End a term with : to match scope only',
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
                'placeholder': 'Comma separated tags. - to exclude, ^ for exact matching.'
                               ' End a term with : to match scope only',
                'data-autocomplete-minimum-characters': 3,
            },
        ),
    )


class Html5DateInput(forms.DateInput):
    input_type = 'date'


class WantedGalleryCreateOrEditForm(ModelForm):

    class Meta:
        model = WantedGallery
        fields = [
            'title', 'title_jpn', 'search_title', 'regexp_search_title',
            'unwanted_title', 'regexp_unwanted_title',
            'wanted_tags', 'unwanted_tags',
            'wanted_tags_exclusive_scope', 'category', 'wanted_page_count_lower', 'wanted_page_count_upper',
            'provider', 'release_date', 'should_search', 'keep_searching', 'reason', 'book_type', 'publisher',
            'page_count'
        ]
        help_texts = {
            'title': 'Informative only',
            'title_jpn': 'Informative only',
            'search_title': 'Text in Gallery title to match',
            'regexp_search_title': 'Use Search Title as a regexp match',
            'unwanted_title': 'Text in Gallery title to exclude',
            'regexp_unwanted_title': 'Use Unwanted Title as a regexp match',
            'wanted_tags': 'Tags in Gallery that must exist',
            'unwanted_tags': 'Tags in Gallery that must not exist',
            'wanted_tags_exclusive_scope': 'Do not accept Galleries that have '
                                           'more than 1 tag in the same wanted tag scope',
            'category': 'Category in Gallery to match',
            'wanted_page_count_lower': 'Gallery must have more or equal than this value (0 is ignored)',
            'wanted_page_count_upper': 'Gallery must have less or equal than this value (0 is ignored)',
            'provider': 'Limit the provider to match (panda, fakku, etc). Default is search in all providers',
            'release_date': 'This Gallery will only be searched when the current date is higher than this value',
            'should_search': 'Enable searching for this Gallery',
            'keep_searching': 'Keep searching for this Gallery after one successful match',
            'reason': 'Informative only',
            'book_type': 'Informative only',
            'publisher': 'Informative only',
            'page_count': 'Informative only'
        }
        widgets = {
            'title': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'title_jpn': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'search_title': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'regexp_search_title': forms.widgets.CheckboxInput(attrs={'class': 'form-control'}),
            'unwanted_title': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'regexp_unwanted_title': forms.widgets.CheckboxInput(attrs={'class': 'form-control'}),
            'wanted_tags': autocomplete.ModelSelect2Multiple(
                url='tag-pk-autocomplete',
                attrs={'data-placeholder': 'Tag name', 'class': 'form-control'}
            ),
            'unwanted_tags': autocomplete.ModelSelect2Multiple(
                url='tag-pk-autocomplete',
                attrs={'data-placeholder': 'Tag name', 'class': 'form-control'}
            ),
            'wanted_tags_exclusive_scope': forms.widgets.CheckboxInput(attrs={'class': 'form-control'}),
            'category': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'wanted_page_count_lower': forms.widgets.NumberInput(attrs={'min': 0, 'class': 'form-control'}),
            'wanted_page_count_upper': forms.widgets.NumberInput(attrs={'min': 0, 'class': 'form-control'}),
            'provider': JalTextWidget(
                url='provider-autocomplete',
                attrs={
                    'class': 'form-control',
                    'placeholder': '',
                    'data-autocomplete-minimum-characters': 3,
                    'data-autocomplete-xhr-wait': 50,
                    'data-autocomplete-auto-hilight-first': 0,
                    'data-autocomplete-bind-mouse-down': 0,
                },
            ),
            'release_date': Html5DateInput(attrs={'class': 'form-control'}),
            'should_search': forms.widgets.CheckboxInput(attrs={'class': 'form-control'}),
            'keep_searching': forms.widgets.CheckboxInput(attrs={'class': 'form-control'}),
            'reason': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'book_type': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'publisher': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'page_count': forms.widgets.NumberInput(attrs={'min': 0, 'class': 'form-control'}),
        }


class ArchiveGroupCreateOrEditForm(ModelForm):

    class Meta:
        model = ArchiveGroup
        fields = [
            'title', 'details', 'position', 'public'
        ]
        widgets = {
            'title': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'details': forms.widgets.Textarea(attrs={'class': 'form-control'}),
            'position': forms.widgets.NumberInput(attrs={'min': 0, 'class': 'form-control'}),
            'public': forms.widgets.CheckboxInput(attrs={'class': 'form-control'})
        }

    def save(self, commit=True):
        archive_group = super(ArchiveGroupCreateOrEditForm, self).save(commit=commit)

        for archive_group_entry in archive_group.archivegroupentry_set.all():
            archive_group_entry.save()
        return archive_group


class ArchiveGroupEntryForm(ModelForm):

    class Meta:
        model = ArchiveGroupEntry
        fields = ['archive', 'title', 'position']
        widgets = {
            'title': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'position': forms.widgets.NumberInput(attrs={'class': 'form-control'}),
            'archive': autocomplete.ModelSelect2(
                url='archive-select-simple-autocomplete',
                attrs={
                    'size': 1, 'data-placeholder': 'Archive', 'class': 'form-control',
                    'data-width': '100%'
                }),
        }


class BaseArchiveGroupEntryFormSet(BaseInlineFormSet):
    def clean(self) -> None:
        super().clean()
        if any(self.errors):
            return
        positions: List[int] = []
        for form in self.forms:
            if 'position' in form.cleaned_data:
                position = form.cleaned_data['position']
                if position in positions:
                    raise forms.ValidationError("Positions must be unique: {}".format(position))
                positions.append(position)


ArchiveGroupEntryFormSet = inlineformset_factory(
    ArchiveGroup, ArchiveGroupEntry, form=ArchiveGroupEntryForm, extra=2,
    formset=BaseArchiveGroupEntryFormSet,
    can_delete=True)


class ArchiveCreateForm(ModelForm):

    class Meta:
        model = Archive
        fields = [
            'zipped', 'title', 'title_jpn', 'gallery', 'source_type', 'reason', 'details'
        ]
        widgets = {
            'zipped': forms.widgets.FileInput(attrs={'class': 'form-control'}),
            'title': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'title_jpn': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'source_type': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'reason': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'gallery': autocomplete.ModelSelect2(
                url='gallery-select-autocomplete',
                attrs={'size': 1, 'data-placeholder': 'Gallery', 'class': 'form-control', 'data-width': '100%'}),
            'details': forms.widgets.Textarea(attrs={'class': 'form-control'}),
        }

    def clean_zipped(self) -> TemporaryUploadedFile:
        zipped = self.cleaned_data['zipped']
        full_path = os.path.join(crawler_settings.MEDIA_ROOT, "galleries/archive_uploads/{file}".format(file=zipped.name))
        if os.path.isfile(full_path):
            raise ValidationError('There is already a file with that name on the file system')
        return zipped


class ArchiveEditForm(ModelForm):

    class Meta:
        model = Archive
        fields = [
            'title', 'title_jpn', 'gallery', 'alternative_sources', 'source_type', 'reason', 'details'
        ]
        widgets = {
            'title': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'title_jpn': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'source_type': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'reason': forms.widgets.TextInput(attrs={'class': 'form-control'}),
            'gallery': autocomplete.ModelSelect2(
                url='gallery-select-autocomplete',
                attrs={'size': 1, 'data-placeholder': 'Gallery', 'class': 'form-control', 'data-width': '100%'}),
            'alternative_sources': autocomplete.ModelSelect2Multiple(
                url='gallery-select-autocomplete',
                attrs={'size': 1, 'data-placeholder': 'Gallery', 'class': 'form-control', 'data-width': '100%'}),
            'details': forms.widgets.Textarea(attrs={'class': 'form-control'}),
        }


class ImageForm(forms.ModelForm):
    position = forms.IntegerField(required=True,
                                  widget=forms.NumberInput(attrs={'size': 10}))

    class Meta:
        model = Image
        fields = ['position']

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(ImageForm, self).__init__(*args, **kwargs)
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
                raise forms.ValidationError("Images positions must be unique: {}".format(position))
            positions.append(position)


ImageFormSet = modelformset_factory(
    Image, form=ImageForm, extra=0,
    formset=BaseImageFormSet,
    can_delete=True)


class BootstrapPasswordChangeForm(PasswordChangeForm):

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields['old_password'].widget.attrs.update({'class': 'form-control'})
        self.fields['new_password1'].widget.attrs.update({'class': 'form-control'})
        self.fields['new_password2'].widget.attrs.update({'class': 'form-control'})


class UserChangeForm(forms.ModelForm):

    class Meta:
        model = User
        fields = ['email']
        widgets = {
            'email': forms.widgets.EmailInput(attrs={'class': 'form-control'}),
        }


class ProfileChangeForm(forms.ModelForm):

    class Meta:
        model = Profile
        fields = ['notify_new_submissions', 'notify_new_private_archive', 'notify_wanted_gallery_found']
        widgets = {
            'notify_new_submissions': forms.widgets.CheckboxInput(attrs={'class': 'form-control'}),
            'notify_new_private_archive': forms.widgets.CheckboxInput(attrs={'class': 'form-control'}),
            'notify_wanted_gallery_found': forms.widgets.CheckboxInput(attrs={'class': 'form-control'}),
        }
