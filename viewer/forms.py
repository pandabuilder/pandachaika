import os
from typing import Any

from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import TemporaryUploadedFile
from django.forms.models import (
    BaseModelFormSet,
    modelformset_factory,
    ModelChoiceField,
    ModelForm,
    inlineformset_factory,
    BaseInlineFormSet,
)
from django.forms.utils import ErrorList
from django.utils.safestring import mark_safe
from django.utils.encoding import force_str
from django.forms.utils import flatatt
from django.conf import settings


from dal import autocomplete

from core.base.utilities import available_filename
from viewer.models import (
    Archive,
    ArchiveMatches,
    Image,
    Gallery,
    Profile,
    WantedGallery,
    ArchiveGroup,
    ArchiveGroupEntry,
    Tag,
    ArchiveManageEntry,
)

from dal.widgets import WidgetMixin

from django.utils.translation import gettext_lazy

crawler_settings = settings.CRAWLER_SETTINGS


class JalTextWidget(WidgetMixin, forms.TextInput):

    class Media:
        js = ("js/vendor/jquery.autocomplete-light.min.js",)

    autocomplete_function = "jal"

    def __init__(self, url=None, forward=None, attrs=None, *args, **kwargs):
        forms.TextInput.__init__(self, *args, **kwargs)

        WidgetMixin.__init__(self, url=url, forward=forward)

        self.attrs.update(attrs)

    def render(self, name, value, attrs=None, renderer=None, **kwargs):
        """Proxy Django's TextInput.render()"""
        html = """
            <input id="{id}" {attrs} type="search" name="{name}" value="{value}" />
        """.format(
            id=attrs["id"],
            name=name,
            value=force_str("" if value is None else value),
            attrs=flatatt(self.build_attrs(attrs)),
        )

        return mark_safe(html)

    def build_attrs(self, *args: Any, **kwargs: Any):

        attrs = {
            "data-autocomplete-choice-selector": "[data-value]",
            "data-widget-bootstrap": "text",
            "data-autocomplete-url": self.url,
            "placeholder": gettext_lazy("type some text to search in this autocomplete"),
        }

        attrs.update(self.attrs)

        if "class" not in attrs.keys():
            attrs["class"] = ""
        attrs["class"] += " autocomplete-light-text-widget autocomplete vTextField"

        return attrs


class MatchesModelChoiceField(ModelChoiceField):
    def label_from_instance(self, obj: Gallery) -> str:  # type: ignore[override]
        first_artist_tag = Tag.objects.first_artist_tag(gallery=obj.id)
        tag_name = ""
        if first_artist_tag:
            tag_name = first_artist_tag.name
        return "{} >> {} >> {} >> {} >> {} >> {}".format(
            tag_name, obj.pk, obj.title, obj.provider, obj.filecount, obj.filesize
        )


class ArchiveGroupSearchForm(forms.Form):

    title = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url="archive-group-autocomplete",
            attrs={
                "class": "form-control",
                "aria-label": "title",
                "placeholder": "Title, mouse click on autocomplete opens it",
                "data-autocomplete-minimum-characters": 3,
                "data-autocomplete-xhr-wait": 50,
                "data-autocomplete-auto-hilight-first": 0,
                "data-autocomplete-bind-mouse-down": 0,
            },
        ),
    )


class ArchiveSearchForm(forms.Form):

    title = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url="archive-autocomplete",
            attrs={
                "class": "form-control",
                "aria-label": "title",
                "placeholder": "Title or Japanese title, mouse click on autocomplete opens it",
                "data-autocomplete-minimum-characters": 3,
                "data-autocomplete-xhr-wait": 50,
                "data-autocomplete-auto-hilight-first": 0,
                "data-autocomplete-bind-mouse-down": 0,
            },
        ),
    )

    tags = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url="tag-autocomplete",
            attrs={
                "class": "form-control",
                "aria-label": "tags",
                "placeholder": "Comma separated tags. - to exclude, ^ for exact matching."
                " End a term with : to match scope only",
                "data-autocomplete-minimum-characters": 3,
            },
        ),
    )

    # For some reason some search request include this NULL character, remove it here.
    def clean_title(self):
        data = self.cleaned_data["title"]
        return data.replace("\x00", "")


class ArchiveSearchSimpleForm(forms.Form):

    filecount_from = forms.IntegerField(
        required=False,
        label="Images",
        widget=forms.NumberInput(attrs={"class": "form-control number-input mr-sm-1", "placeholder": "from"}),
    )
    filecount_to = forms.IntegerField(
        required=False,
        label="",
        widget=forms.NumberInput(attrs={"class": "form-control number-input mr-sm-1", "placeholder": "to"}),
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
        label="Posted",
        widget=forms.DateInput(
            attrs={"class": "form-control mr-sm-1", "placeholder": "from", "type": "date", "size": 9}
        ),
        input_formats=["%Y-%m-%d"],
    )
    posted_to = forms.DateTimeField(
        required=False,
        label="",
        widget=forms.DateInput(attrs={"class": "form-control mr-sm-1", "placeholder": "to", "type": "date", "size": 9}),
        input_formats=["%Y-%m-%d"],
    )
    source_type = forms.CharField(
        required=False,
        label="Source",
        widget=JalTextWidget(
            url="source-autocomplete",
            attrs={
                "class": "form-control mr-sm-1",
                "placeholder": "",
                "data-autocomplete-minimum-characters": 0,
                "size": 10,
            },
        ),
    )

    reason = forms.CharField(
        required=False,
        label="Reason",
        widget=JalTextWidget(
            url="reason-autocomplete",
            attrs={
                "class": "form-control mr-sm-1",
                "placeholder": "",
                "data-autocomplete-minimum-characters": 0,
                "size": 10,
            },
        ),
    )

    uploader = forms.CharField(
        required=False,
        label="Uploader",
        widget=JalTextWidget(
            url="uploader-autocomplete",
            attrs={
                "class": "form-control mr-sm-1",
                "placeholder": "",
                "data-autocomplete-minimum-characters": 3,
                "size": 10,
            },
        ),
    )

    category = forms.CharField(
        required=False,
        label="Category",
        widget=JalTextWidget(
            url="category-autocomplete",
            attrs={
                "class": "form-control",
                "placeholder": "",
                "data-autocomplete-minimum-characters": 0,
                "size": 10,
            },
        ),
    )

    filesize_from = forms.IntegerField(
        required=False,
        label="Size",
        widget=forms.NumberInput(attrs={"class": "form-control mr-sm-1", "placeholder": "from"}),
    )
    filesize_to = forms.IntegerField(
        required=False, label="", widget=forms.NumberInput(attrs={"class": "form-control mr-sm-1", "placeholder": "to"})
    )


class ArchiveManageSearchSimpleForm(forms.Form):

    filecount_from = forms.IntegerField(
        required=False,
        label="Images",
        widget=forms.NumberInput(attrs={"class": "form-control number-input mr-sm-1", "placeholder": "from"}),
    )
    filecount_to = forms.IntegerField(
        required=False,
        label="",
        widget=forms.NumberInput(attrs={"class": "form-control number-input mr-sm-1", "placeholder": "to"}),
    )
    created_from = forms.DateTimeField(
        required=False,
        label="Created",
        widget=forms.DateInput(
            attrs={"class": "form-control mr-sm-1", "placeholder": "from", "type": "date", "size": 9}
        ),
        input_formats=["%Y-%m-%d"],
    )
    created_to = forms.DateTimeField(
        required=False,
        label="",
        widget=forms.DateInput(attrs={"class": "form-control mr-sm-1", "placeholder": "to", "type": "date", "size": 9}),
        input_formats=["%Y-%m-%d"],
    )
    source_type = forms.CharField(
        required=False,
        label="",
        widget=JalTextWidget(
            url="source-autocomplete",
            attrs={
                "class": "form-control mr-sm-1",
                "placeholder": "Source",
                "data-autocomplete-minimum-characters": 0,
                "size": 12,
            },
        ),
    )

    reason = forms.CharField(
        required=False,
        label="",
        widget=JalTextWidget(
            url="reason-autocomplete",
            attrs={
                "class": "form-control mr-sm-1",
                "placeholder": "Reason",
                "data-autocomplete-minimum-characters": 0,
                "size": 12,
            },
        ),
    )

    category = forms.CharField(
        required=False,
        label="",
        widget=JalTextWidget(
            url="category-autocomplete",
            attrs={
                "class": "form-control",
                "placeholder": "Category",
                "data-autocomplete-minimum-characters": 0,
                "size": 12,
            },
        ),
    )

    provider = forms.CharField(
        required=False,
        label="",
        widget=JalTextWidget(
            url="provider-autocomplete",
            attrs={
                "class": "form-control",
                "placeholder": "Provider",
                "data-autocomplete-minimum-characters": 0,
                "size": 12,
            },
        ),
    )

    gallery__status = forms.ChoiceField(
        choices=[(0, "--------")] + Gallery.StatusChoices.choices,
        required=False,
        label="Gallery Status",
        widget=forms.widgets.Select(attrs={"class": "form-control"}),
    )

    extra_files = forms.CharField(
        required=False,
        label="Extra files",
        widget=forms.widgets.TextInput(attrs={"class": "form-control"}),
    )


class DivErrorList(ErrorList):

    template_name = "viewer/forms/errors_as_div.html"


class ArchiveGroupSelectForm(forms.Form):

    archive_group = forms.ModelMultipleChoiceField(
        required=False,
        queryset=ArchiveGroup.objects.none(),
        widget=autocomplete.ModelSelect2Multiple(
            url="archive-group-select-autocomplete",
            attrs={"size": 1, "data-placeholder": "Group name", "class": "form-control"},
        ),
    )


class ArchiveModForm(forms.ModelForm):

    class Meta:
        model = Archive
        fields = [
            "title",
            "title_jpn",
            "source_type",
            "reason",
            "possible_matches",
            "tags",
            "zipped",
            "alternative_sources",
            "archive_groups",
            "details",
        ]
        widgets = {
            "tags": autocomplete.ModelSelect2Multiple(
                url="customtag-autocomplete",
                attrs={"size": 1, "data-placeholder": "Custom tag name", "class": "form-control", "data-width": "100%"},
            ),
            "alternative_sources": autocomplete.ModelSelect2Multiple(
                url="gallery-select-autocomplete",
                attrs={
                    "size": 1,
                    "data-placeholder": "Alternative source",
                    "class": "form-control",
                    "data-width": "100%",
                },
            ),
            "title": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "title_jpn": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "source_type": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "reason": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "zipped": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "details": forms.widgets.Textarea(attrs={"class": "form-control"}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(ArchiveModForm, self).__init__(*args, **kwargs)
        self.fields["possible_matches"].queryset = self.instance.possible_matches.order_by(  # type: ignore
            "-archivematches__match_accuracy"
        )
        self.fields["archive_groups"].queryset = self.instance.archive_groups.all()  # type: ignore
        self.fields["tags"].label = "Custom Tags"
        self.fields["tags"].queryset = self.instance.custom_tags()  # type: ignore

    possible_matches = MatchesModelChoiceField(
        required=False,
        queryset=ArchiveMatches.objects.none(),
        widget=forms.widgets.Select(attrs={"class": "form-control"}),
    )

    archive_groups = forms.models.ModelMultipleChoiceField(
        required=False,
        queryset=ArchiveGroup.objects.none(),
        widget=autocomplete.ModelSelect2Multiple(
            url="archive-group-select-autocomplete",
            attrs={"size": 1, "data-placeholder": "Archive groups", "class": "form-control", "data-width": "100%"},
        ),
    )


class GallerySearchForm(forms.Form):

    title = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url="gallery-autocomplete",
            attrs={
                "class": "form-control",
                "placeholder": "Title, Japanese title or ex gallery ID, mouse click on autocomplete opens it",
                "data-autocomplete-minimum-characters": 3,
                "data-autocomplete-xhr-wait": 50,
                "data-autocomplete-auto-hilight-first": 0,
                "data-autocomplete-bind-mouse-down": 0,
            },
        ),
    )

    tags = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url="tag-autocomplete",
            attrs={
                "class": "form-control",
                "placeholder": "Comma separated tags. - to exclude, ^ for exact matching."
                " End a term with : to match scope only",
                "data-autocomplete-minimum-characters": 3,
            },
        ),
    )


class GallerySearchSimpleForm(forms.Form):

    filecount_from = forms.IntegerField(
        required=False,
        label="Images",
        widget=forms.NumberInput(attrs={"class": "form-control number-input mr-sm-1", "placeholder": "from"}),
    )
    filecount_to = forms.IntegerField(
        required=False,
        label="",
        widget=forms.NumberInput(attrs={"class": "form-control number-input mr-sm-1", "placeholder": "to"}),
    )
    posted_from = forms.DateTimeField(
        required=False,
        label="Posted",
        widget=forms.DateInput(
            attrs={"class": "form-control mr-sm-1", "placeholder": "from", "type": "date", "size": 9}
        ),
        input_formats=["%Y-%m-%d"],
    )
    posted_to = forms.DateTimeField(
        required=False,
        label="",
        widget=forms.DateInput(attrs={"class": "form-control mr-sm-1", "placeholder": "to", "type": "date", "size": 9}),
        input_formats=["%Y-%m-%d"],
    )
    provider = forms.CharField(
        required=False,
        label="Source",
        widget=JalTextWidget(
            url="gallery-provider-autocomplete",
            attrs={
                "class": "form-control mr-sm-1",
                "placeholder": "panda, etc.",
                "data-autocomplete-minimum-characters": 0,
                "size": 10,
            },
        ),
    )

    reason = forms.CharField(
        required=False,
        label="Reason",
        widget=JalTextWidget(
            url="gallery-reason-autocomplete",
            attrs={
                "class": "form-control mr-sm-1",
                "placeholder": "wani, etc.",
                "data-autocomplete-minimum-characters": 0,
                "size": 10,
            },
        ),
    )

    uploader = forms.CharField(
        required=False,
        label="Uploader",
        widget=JalTextWidget(
            url="gallery-uploader-autocomplete",
            attrs={
                "class": "form-control mr-sm-1",
                "placeholder": "",
                "data-autocomplete-minimum-characters": 3,
                "size": 10,
            },
        ),
    )

    category = forms.CharField(
        required=False,
        label="Category",
        widget=JalTextWidget(
            url="gallery-category-autocomplete",
            attrs={
                "class": "form-control",
                "placeholder": "",
                "data-autocomplete-minimum-characters": 0,
                "size": 10,
            },
        ),
    )

    filesize_from = forms.IntegerField(
        required=False,
        label="Size",
        widget=forms.NumberInput(attrs={"class": "form-control mr-sm-1", "placeholder": "from"}),
    )
    filesize_to = forms.IntegerField(
        required=False, label="", widget=forms.NumberInput(attrs={"class": "form-control mr-sm-1", "placeholder": "to"})
    )

    status = forms.ChoiceField(
        choices=[(0, "--------")] + Gallery.StatusChoices.choices,
        required=False,
        label="Status",
        widget=forms.widgets.Select(attrs={"class": "form-control"}),
    )


class WantedGallerySearchForm(forms.Form):

    title = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url="wanted-gallery-autocomplete",
            attrs={
                "class": "form-control",
                "placeholder": "Title, Japanese title or search title, mouse click on autocomplete opens it",
                "data-autocomplete-minimum-characters": 3,
                "data-autocomplete-xhr-wait": 50,
                "data-autocomplete-auto-hilight-first": 0,
                "data-autocomplete-bind-mouse-down": 0,
            },
        ),
    )

    tags = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url="tag-autocomplete",
            attrs={
                "class": "form-control",
                "placeholder": "Comma separated tags. - to exclude, ^ for exact matching."
                " End a term with : to match scope only",
                "data-autocomplete-minimum-characters": 3,
            },
        ),
    )


class WantedGalleryColSearchForm(WantedGallerySearchForm):
    title = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url="col-wanted-gallery-autocomplete",
            attrs={
                "class": "form-control",
                "placeholder": "Title, Japanese title or search title, mouse click on autocomplete opens it",
                "data-autocomplete-minimum-characters": 3,
                "data-autocomplete-xhr-wait": 50,
                "data-autocomplete-auto-hilight-first": 0,
                "data-autocomplete-bind-mouse-down": 0,
            },
        ),
    )


class EmptyChoiceField(forms.ChoiceField):
    def __init__(
        self,
        choices=(),
        empty_label=None,
        required=True,
        widget=None,
        label=None,
        initial=None,
        help_text=None,
        *args,
        **kwargs,
    ):

        # prepend an empty label if it exists (and field is not required!)
        if not required and empty_label is not None:
            choices = tuple([("", empty_label)] + list(choices))

        super(EmptyChoiceField, self).__init__(
            choices=choices,
            required=required,
            widget=widget,
            label=label,
            initial=initial,
            help_text=help_text,
            *args,
            **kwargs,
        )


class ArchiveManageEntrySimpleForm(forms.Form):

    mark_reason = forms.CharField(
        required=False,
        label="Reason",
        widget=forms.widgets.TextInput(attrs={"class": "form-control"}),
    )

    mark_comment = forms.CharField(
        required=False,
        label="Comment",
        widget=forms.widgets.TextInput(attrs={"class": "form-control"}),
    )

    origin = EmptyChoiceField(
        choices=ArchiveManageEntry.ORIGIN_CHOICES,
        required=False,
        empty_label="",
        widget=forms.widgets.Select(attrs={"class": "form-control"}),
    )

    mark_extra = forms.CharField(
        required=False,
        label="Extra",
        widget=forms.widgets.TextInput(attrs={"class": "form-control"}),
    )

    priority_from = forms.IntegerField(
        required=False,
        label="Priority",
        widget=forms.NumberInput(attrs={"class": "form-control mr-sm-1", "placeholder": "from", "size": 9}),
    )
    priority_to = forms.IntegerField(
        required=False,
        label="",
        widget=forms.NumberInput(attrs={"class": "form-control mr-sm-1", "placeholder": "to", "size": 9}),
    )


class Html5DateInput(forms.DateInput):
    input_type = "date"


class WantedGalleryCreateOrEditForm(ModelForm):

    class Meta:
        model = WantedGallery
        fields = [
            "title",
            "title_jpn",
            "search_title",
            "regexp_search_title",
            "regexp_search_title_icase",
            "unwanted_title",
            "regexp_unwanted_title",
            "regexp_unwanted_title_icase",
            "wanted_tags",
            "unwanted_tags",
            "wanted_providers",
            "unwanted_providers",
            "wanted_tags_exclusive_scope",
            "exclusive_scope_name",
            "wanted_tags_accept_if_none_scope",
            "category",
            "categories",
            "wanted_page_count_lower",
            "wanted_page_count_upper",
            "add_to_archive_group",
            "release_date",
            "wait_for_time",
            "should_search",
            "keep_searching",
            "reason",
            "book_type",
            "publisher",
            "page_count",
            "restricted_to_links",
        ]
        help_texts = {
            "title": "Informative only",
            "title_jpn": "Informative only",
            "search_title": "Text in Gallery title to match",
            "regexp_search_title": "Use Search Title as a regexp match",
            "regexp_search_title_icase": "Case-insensitive use of regexp Search Title",
            "unwanted_title": "Text in Gallery title to exclude",
            "regexp_unwanted_title": "Use Unwanted Title as a regexp match",
            "regexp_unwanted_title_icase": "Case-insensitive use of regexp Unwanted Title",
            "wanted_tags": "Tags in Gallery that must exist (AND logic)",
            "unwanted_tags": "Tags in Gallery that must not exist (OR logic)",
            "wanted_tags_exclusive_scope": "Do not accept Galleries that have "
            "more than 1 tag in the same wanted tag scope",
            "exclusive_scope_name": "Exclusive scope filter is restringed to an specific scope",
            "wanted_tags_accept_if_none_scope": "Scope from wanted tags that is either present"
            " with the tag names or not at all",
            "category": "Category in Gallery to match",
            "categories": "Category in Gallery to match (OR logic)",
            "wanted_page_count_lower": "Gallery must have more or equal than this value (0 is ignored)",
            "wanted_page_count_upper": "Gallery must have less or equal than this value (0 is ignored)",
            "add_to_archive_group": "ArchiveGroup to add the resulting Archive if found",
            "wanted_providers": "Limit the provider to match (panda, fakku, etc). Default is search in all providers",
            "unwanted_providers": "Exclude galleries from these providers (panda, fakku, etc)",
            "wait_for_time": "Time after gallery is posted to wait for it to be considered, in timedelta format."
            " Useful if waiting for unwanted tags",
            "release_date": "This Gallery will only be searched when the current date is higher than this value",
            "should_search": "Enable searching for this Gallery",
            "keep_searching": "Keep searching for this Gallery after one successful match",
            "reason": "Informative only",
            "book_type": "Informative only",
            "publisher": "Informative only",
            "page_count": "Informative only",
            "restricted_to_links": "Only allow matching against specified MonitoredLinks",
        }
        widgets = {
            "title": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "title_jpn": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "search_title": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "regexp_search_title": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
            "regexp_search_title_icase": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
            "unwanted_title": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "regexp_unwanted_title": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
            "regexp_unwanted_title_icase": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
            "wanted_tags": autocomplete.ModelSelect2Multiple(
                url="tag-pk-autocomplete",
                attrs={"data-placeholder": "Tag name", "class": "form-control", "data-width": "100%"},
            ),
            "unwanted_tags": autocomplete.ModelSelect2Multiple(
                url="tag-pk-autocomplete",
                attrs={"data-placeholder": "Tag name", "class": "form-control", "data-width": "100%"},
            ),
            "wanted_tags_exclusive_scope": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
            "exclusive_scope_name": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "wanted_tags_accept_if_none_scope": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "category": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "categories": autocomplete.ModelSelect2Multiple(
                url="category-pk-autocomplete",
                attrs={"data-placeholder": "Category", "class": "form-control", "data-width": "100%"},
            ),
            "wanted_page_count_lower": forms.widgets.NumberInput(attrs={"min": 0, "class": "form-control"}),
            "wanted_page_count_upper": forms.widgets.NumberInput(attrs={"min": 0, "class": "form-control"}),
            "wanted_providers": autocomplete.ModelSelect2Multiple(
                url="provider-pk-autocomplete",
                attrs={"data-placeholder": "Provider name", "class": "form-control", "data-width": "100%"},
            ),
            "unwanted_providers": autocomplete.ModelSelect2Multiple(
                url="provider-pk-autocomplete",
                attrs={"data-placeholder": "Provider name", "class": "form-control", "data-width": "100%"},
            ),
            "wait_for_time": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "release_date": Html5DateInput(attrs={"class": "form-control"}),
            "should_search": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
            "keep_searching": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
            "reason": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "book_type": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "publisher": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "page_count": forms.widgets.NumberInput(attrs={"min": 0, "class": "form-control"}),
            "add_to_archive_group": autocomplete.ModelSelect2(
                url="archive-group-select-autocomplete",
                attrs={"size": 1, "data-placeholder": "Archive Group", "class": "form-control"},
            ),
            "restricted_to_links": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class ArchiveGroupCreateOrEditForm(ModelForm):

    class Meta:
        model = ArchiveGroup
        fields = ["title", "details", "position", "public"]
        widgets = {
            "title": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "details": forms.widgets.Textarea(attrs={"class": "form-control"}),
            "position": forms.widgets.NumberInput(attrs={"min": 0, "class": "form-control"}),
            "public": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def save(self, commit=True):
        archive_group = super(ArchiveGroupCreateOrEditForm, self).save(commit=commit)

        for archive_group_entry in archive_group.archivegroupentry_set.all():
            archive_group_entry.save()
        return archive_group


class ArchiveGroupEntryForm(ModelForm):
    # template_name_div = "form_snippet.html"
    class Meta:
        model = ArchiveGroupEntry
        fields = ["archive", "title"]
        widgets = {
            "title": forms.widgets.TextInput(attrs={"class": "form-control"}),
            # 'position': forms.widgets.NumberInput(attrs={'class': 'form-control'}),
            "archive": autocomplete.ModelSelect2(
                url="archive-select-simple-autocomplete",
                attrs={"size": 1, "data-placeholder": "Archive", "class": "form-control", "data-width": "100%"},
            ),
        }


class BaseArchiveGroupEntryFormSet(BaseInlineFormSet):
    def get_ordering_widget(self):
        return forms.widgets.NumberInput(attrs={"min": 0, "class": "form-control"})

    def clean(self) -> None:
        super().clean()
        if any(self.errors):
            return
        positions: list[int] = []
        for form in self.forms:
            if "ORDER" in form.cleaned_data:
                position = form.cleaned_data["ORDER"]
                if position in positions:
                    raise forms.ValidationError("Positions must be unique: {}".format(position))
                positions.append(position)


ArchiveGroupEntryFormSet = inlineformset_factory(
    ArchiveGroup,
    ArchiveGroupEntry,
    form=ArchiveGroupEntryForm,
    extra=2,
    formset=BaseArchiveGroupEntryFormSet,
    can_delete=True,
    can_order=True,
)


class ArchiveCreateForm(ModelForm):

    class Meta:
        model = Archive
        fields = ["zipped", "title", "title_jpn", "gallery", "source_type", "reason", "details"]
        widgets = {
            "zipped": forms.widgets.FileInput(attrs={"class": "form-control"}),
            "title": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "title_jpn": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "source_type": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "reason": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "gallery": autocomplete.ModelSelect2(
                url="gallery-select-autocomplete",
                attrs={"size": 1, "data-placeholder": "Gallery", "class": "form-control", "data-width": "100%"},
            ),
            "details": forms.widgets.Textarea(attrs={"class": "form-control"}),
        }

    def clean_zipped(self) -> TemporaryUploadedFile:
        zipped = self.cleaned_data["zipped"]

        available_path = available_filename(
            crawler_settings.MEDIA_ROOT, "galleries/archive_uploads/{file}".format(file=zipped.name)
        )

        full_path = os.path.join(crawler_settings.MEDIA_ROOT, available_path)

        if os.path.isfile(full_path):
            raise ValidationError("There is already a file with that name on the file system")
        return zipped


class ArchiveEditForm(ModelForm):
    old_gallery_to_alt = forms.BooleanField(
        initial=False, required=False, help_text="When changing the main Gallery, add the old one as Alternative Source"
    )

    freeze_titles = forms.BooleanField(
        initial=False,
        required=False,
        help_text="When an associated gallery updates its titles, don't update this Archive's titles.",
    )

    freeze_tags = forms.BooleanField(
        initial=False,
        required=False,
        help_text="When an associated gallery updates its tags, don't update this Archive's gallery tags.",
    )

    class Meta:
        model = Archive
        fields = ["title", "title_jpn", "gallery", "alternative_sources", "source_type", "reason", "details"]
        widgets = {
            "title": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "title_jpn": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "source_type": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "reason": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "gallery": autocomplete.ModelSelect2(
                url="gallery-select-autocomplete",
                attrs={"size": 1, "data-placeholder": "Gallery", "class": "form-control", "data-width": "100%"},
            ),
            "alternative_sources": autocomplete.ModelSelect2Multiple(
                url="gallery-select-autocomplete",
                attrs={"size": 1, "data-placeholder": "Gallery", "class": "form-control", "data-width": "100%"},
            ),
            "details": forms.widgets.Textarea(attrs={"class": "form-control"}),
        }


class ArchiveManageEditForm(ModelForm):

    class Meta:
        model = ArchiveManageEntry
        fields = [
            # 'mark_check',
            "mark_priority",
            "mark_reason",
            "mark_comment",
            "mark_extra",
            # 'resolve_check', 'resolve_comment'
        ]
        widgets = {
            # 'mark_check': forms.widgets.CheckboxInput(attrs={'class': 'form-check-input'}),
            "mark_priority": forms.widgets.NumberInput(attrs={"class": "form-control"}),
            "mark_reason": JalTextWidget(
                url="archive-manager-reason-autocomplete",
                attrs={
                    "class": "form-control",
                    "placeholder": "",
                    "data-autocomplete-minimum-characters": 0,
                    "size": 10,
                },
            ),
            "mark_comment": forms.widgets.Textarea(attrs={"class": "form-control"}),
            "mark_extra": forms.widgets.TextInput(attrs={"class": "form-control"}),
            # 'resolve_check': forms.widgets.CheckboxInput(attrs={'class': 'form-check-input'}),
            # 'resolve_comment': forms.widgets.Textarea(attrs={'class': 'form-control'})
        }
        help_texts = {
            "mark_priority": "0 to 1, low priority. 1 to 4, mid priority. 4 to 5, high priority. "
            "Low priority is hidden on search results by default.",
        }


ArchiveManageEditFormSet = inlineformset_factory(
    Archive, ArchiveManageEntry, form=ArchiveManageEditForm, extra=1, can_delete=True
)


class ImageForm(forms.ModelForm):
    position = forms.IntegerField(required=True, widget=forms.NumberInput(attrs={"size": 10}))

    class Meta:
        model = Image
        fields = ["position"]

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
        positions: list[int] = []
        for form in self.forms:
            position = form.cleaned_data["position"]
            if position in positions:
                raise forms.ValidationError("Images positions must be unique: {}".format(position))
            positions.append(position)


ImageFormSet = modelformset_factory(Image, form=ImageForm, extra=0, formset=BaseImageFormSet, can_delete=True)


class BootstrapPasswordChangeForm(PasswordChangeForm):

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["old_password"].widget.attrs.update({"class": "form-control"})
        self.fields["new_password1"].widget.attrs.update({"class": "form-control"})
        self.fields["new_password2"].widget.attrs.update({"class": "form-control"})


class UserChangeForm(forms.ModelForm):

    class Meta:
        model = User
        fields = ["email"]
        widgets = {
            "email": forms.widgets.EmailInput(attrs={"class": "form-control"}),
        }


class ProfileChangeForm(forms.ModelForm):

    def __init__(self, *args, **kwargs):
        super(ProfileChangeForm, self).__init__(*args, **kwargs)
        self.label_suffix = ""

    class Meta:
        model = Profile
        fields = ["notify_new_submissions", "notify_new_private_archive", "notify_wanted_gallery_found"]
        widgets = {
            "notify_new_submissions": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
            "notify_new_private_archive": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
            "notify_wanted_gallery_found": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class GalleryCreateForm(ModelForm):

    class Meta:
        model = Gallery
        fields = [
            "gid",
            "token",
            "title",
            "title_jpn",
            "tags",
            "gallery_container",
            "magazine",
            "category",
            "uploader",
            "comment",
            "posted",
            "filecount",
            "filesize",
            "expunged",
            "disowned",
            "hidden",
            "fjord",
            "provider",
            "reason",
            "thumbnail_url",
            "thumbnail",
        ]
        widgets = {
            "gid": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "provider": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "token": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "title": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "title_jpn": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "gallery_container": autocomplete.ModelSelect2(
                url="gallery-select-autocomplete",
                attrs={"size": 1, "data-placeholder": "Gallery", "class": "form-control", "data-width": "100%"},
            ),
            "magazine": autocomplete.ModelSelect2(
                url="gallery-select-autocomplete",
                attrs={"size": 1, "data-placeholder": "Gallery", "class": "form-control", "data-width": "100%"},
            ),
            "tags": autocomplete.ModelSelect2Multiple(
                url="tag-pk-autocomplete",
                attrs={"data-placeholder": "Tag name", "class": "form-control", "data-width": "100%"},
            ),
            "category": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "uploader": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "comment": forms.widgets.Textarea(attrs={"class": "form-control"}),
            "posted": forms.DateInput(attrs={"class": "form-control mr-sm-1", "type": "date", "size": 9}),
            "filecount": forms.widgets.NumberInput(attrs={"class": "form-control"}),
            "filesize": forms.widgets.NumberInput(attrs={"class": "form-control"}),
            "expunged": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
            "disowned": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
            "hidden": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
            "fjord": forms.widgets.CheckboxInput(attrs={"class": "form-check-input"}),
            "reason": forms.widgets.TextInput(attrs={"class": "form-control"}),
            "thumbnail_url": forms.widgets.URLInput(attrs={"class": "form-control"}),
            "thumbnail": forms.widgets.ClearableFileInput(attrs={"class": "form-control"}),
        }

        help_texts = {
            "thumbnail": "If this field is empty, the thumbnail will be fetched from the thumbnail URL",
        }

    def clean_tags(self) -> TemporaryUploadedFile:
        tags = self.cleaned_data["tags"]
        return tags


class EventLogSearchForm(forms.Form):

    data_field = forms.CharField(
        required=False,
        label="",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Data", "autocomplete": "off"}),
    )


class AllGalleriesSearchForm(forms.Form):

    title = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url="gallery-all-autocomplete",
            attrs={
                "class": "form-control",
                "placeholder": "Title, Japanese title or ex gallery ID, mouse click on autocomplete opens it",
                "data-autocomplete-minimum-characters": 3,
                "data-autocomplete-xhr-wait": 50,
                "data-autocomplete-auto-hilight-first": 0,
                "data-autocomplete-bind-mouse-down": 0,
            },
        ),
    )

    tags = forms.CharField(
        required=False,
        widget=JalTextWidget(
            url="tag-autocomplete",
            attrs={
                "class": "form-control",
                "placeholder": "Comma separated tags. - to exclude, ^ for exact matching."
                " End a term with : to match scope only",
                "data-autocomplete-minimum-characters": 3,
            },
        ),
    )


class EventSearchForm(forms.Form):

    data_field = forms.CharField(
        required=False,
        label="",
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "Data", "autocomplete": "off", "size": 45}
        ),
    )

    event_date_from = forms.DateTimeField(
        required=False,
        label="When",
        widget=forms.DateInput(
            attrs={"class": "form-control mr-sm-1", "placeholder": "from", "type": "date", "size": 9}
        ),
        input_formats=["%Y-%m-%d"],
    )
    event_date_to = forms.DateTimeField(
        required=False,
        label="",
        widget=forms.DateInput(attrs={"class": "form-control mr-sm-1", "placeholder": "to", "type": "date", "size": 9}),
        input_formats=["%Y-%m-%d"],
    )

    content_type = forms.CharField(
        required=False,
        label="",
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "Content Type", "autocomplete": "off", "size": 10}
        ),
    )

    content_id = forms.CharField(
        required=False,
        label="",
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "Content Id", "autocomplete": "off", "size": 9}
        ),
    )


class SplitArchiveForm(forms.ModelForm):
    new_file_name = forms.CharField(widget=forms.widgets.TextInput(attrs={"class": "form-control", "size": 100}))
    starting_position = forms.IntegerField(
        required=True, widget=forms.NumberInput(attrs={"size": 10, "min": 0, "class": "form-control"})
    )
    ending_position = forms.IntegerField(
        required=True, widget=forms.NumberInput(attrs={"size": 10, "min": 0, "class": "form-control"})
    )

    class Meta:
        model = Archive
        fields: list[str] = []


class BaseSplitArchiveFormSet(BaseModelFormSet):

    def __init__(self, *args, **kwargs):
        self.filecount = kwargs.pop("filecount")
        super(BaseSplitArchiveFormSet, self).__init__(*args, **kwargs)

    def clean(self) -> None:
        if any(self.errors):
            return
        positions: list[int] = [0] * self.filecount
        for form in self.forms:
            if not form.cleaned_data:
                continue
            starting_position = form.cleaned_data["starting_position"]
            ending_position = form.cleaned_data["ending_position"]
            if starting_position < 1 or ending_position > self.filecount:
                raise forms.ValidationError(
                    "Archive position out of bounds (1-index): [{}, {}]".format(starting_position, ending_position)
                )
            positions = [
                x[1] + 1 if x[0] >= starting_position - 1 and x[0] <= ending_position - 1 else x[1]
                for x in enumerate(positions)
            ]
            overlapping_positions = [x for x in positions if x > 1]
            if overlapping_positions:
                raise forms.ValidationError(
                    "The following positions are overlapping: {}".format(
                        ",".join([str(x) for x in overlapping_positions])
                    )
                )


SplitArchiveFormSet = modelformset_factory(
    Archive, form=SplitArchiveForm, extra=5, formset=BaseSplitArchiveFormSet, can_delete=False
)
