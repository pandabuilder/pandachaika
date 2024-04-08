from typing import Union

from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, re_path, URLPattern, URLResolver

from django.contrib import admin

from viewer.views.complete import (
    ArchiveAutocomplete, TagAutocomplete,
    NonCustomTagAutocomplete, CustomTagAutocomplete,
    GalleryAutocomplete, SourceAutocomplete,
    ReasonAutocomplete, UploaderAutocomplete,
    WantedGalleryAutocomplete, ProviderPkAutocomplete,
    CategoryAutocomplete, ProviderAutocomplete, TagPkAutocomplete, GallerySelectAutocomplete, ArchiveSelectAutocomplete,
    ArchiveGroupAutocomplete, ArchiveGroupSelectAutocomplete, ArchiveSelectSimpleAutocomplete,
    GalleryProviderAutocomplete, GalleryCategoryAutocomplete, GalleryUploaderAutocomplete, GalleryReasonAutocomplete,
    WantedGalleryColAutocomplete, ArchiveManageEntryMarkReasonAutocomplete, GalleryAllAutocomplete, TagAutocompleteJson)

admin.autodiscover()

urlpatterns: list[Union[URLPattern, URLResolver]] = [
    re_path(r'^' + settings.MAIN_URL + r'admin/', admin.site.urls),
]

urlpatterns += [
    re_path(
        r'^' + settings.MAIN_URL + r'archive-autocomplete/$',
        ArchiveAutocomplete.as_view(),
        name='archive-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'archive-group-autocomplete/$',
        ArchiveGroupAutocomplete.as_view(),
        name='archive-group-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'archive-group-select-autocomplete/$',
        ArchiveGroupSelectAutocomplete.as_view(create_field='title'),
        name='archive-group-select-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'archive-select-simple-autocomplete/$',
        ArchiveSelectSimpleAutocomplete.as_view(),
        name='archive-select-simple-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'archive-select-autocomplete/$',
        ArchiveSelectAutocomplete.as_view(),
        name='archive-select-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'gallery-autocomplete/$',
        GalleryAutocomplete.as_view(),
        name='gallery-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'gallery-all-autocomplete/$',
        GalleryAllAutocomplete.as_view(),
        name='gallery-all-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'gallery-select-autocomplete/$',
        GallerySelectAutocomplete.as_view(),
        name='gallery-select-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'tag-autocomplete/$',
        TagAutocomplete.as_view(),
        name='tag-autocomplete',
    ),
re_path(
        r'^' + settings.MAIN_URL + r'tag-json-autocomplete/$',
        TagAutocompleteJson.as_view(),
        name='tag-json-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'tag-pk-autocomplete/$',
        TagPkAutocomplete.as_view(),
        name='tag-pk-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'noncustomtag-autocomplete/$',
        NonCustomTagAutocomplete.as_view(),
        name='noncustomtag-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'customtag-autocomplete/$',
        CustomTagAutocomplete.as_view(create_field='name'),
        name='customtag-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'wanted-gallery-autocomplete/$',
        WantedGalleryAutocomplete.as_view(),
        name='wanted-gallery-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'col-wanted-gallery-autocomplete/$',
        WantedGalleryColAutocomplete.as_view(),
        name='col-wanted-gallery-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'source-autocomplete/$',
        SourceAutocomplete.as_view(),
        name='source-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'provider-autocomplete/$',
        ProviderAutocomplete.as_view(),
        name='provider-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'provider-pk-autocomplete/$',
        ProviderPkAutocomplete.as_view(),
        name='provider-pk-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'reason-autocomplete/$',
        ReasonAutocomplete.as_view(),
        name='reason-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'uploader-autocomplete/$',
        UploaderAutocomplete.as_view(),
        name='uploader-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'category-autocomplete/$',
        CategoryAutocomplete.as_view(),
        name='category-autocomplete',
    ),
    # Gallery
    re_path(
        r'^' + settings.MAIN_URL + r'gallery-provider-autocomplete/$',
        GalleryProviderAutocomplete.as_view(),
        name='gallery-provider-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'gallery-category-autocomplete/$',
        GalleryCategoryAutocomplete.as_view(),
        name='gallery-category-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'gallery-uploader-autocomplete/$',
        GalleryUploaderAutocomplete.as_view(),
        name='gallery-uploader-autocomplete',
    ),
    re_path(
        r'^' + settings.MAIN_URL + r'gallery-reason-autocomplete/$',
        GalleryReasonAutocomplete.as_view(),
        name='gallery-reason-autocomplete',
    ),
    # Other
    re_path(
        r'^' + settings.MAIN_URL + r'archive-manager-reason-autocomplete/$',
        ArchiveManageEntryMarkReasonAutocomplete.as_view(),
        name='archive-manager-reason-autocomplete',
    ),
]

if settings.DEBUG:
    try:
        import debug_toolbar
        urlpatterns += [
            re_path(r'^' + settings.MAIN_URL + r'__debug__/', include(debug_toolbar.urls)),
        ]
        urlpatterns += static(settings.STATIC_URL,
                              document_root=settings.STATIC_ROOT)
        urlpatterns += static(settings.MEDIA_URL,
                              document_root=settings.MEDIA_ROOT)
    except ImportError:
        debug_toolbar = None


urlpatterns += [
    re_path(r'^' + settings.MAIN_URL, include('viewer.urls')),
]
