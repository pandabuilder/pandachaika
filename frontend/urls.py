from django.conf.urls import include, url
from django.conf import settings
from django.conf.urls.static import static

from django.contrib import admin

from viewer.views.complete import (
    ArchiveAutocomplete, TagAutocomplete,
    NonCustomTagAutocomplete, CustomTagAutocomplete,
    GalleryAutocomplete, SourceAutocomplete,
    ReasonAutocomplete, UploaderAutocomplete,
    WantedGalleryAutocomplete,
    CategoryAutocomplete, ProviderAutocomplete, TagPkAutocomplete, GallerySelectAutocomplete)

admin.autodiscover()

urlpatterns = [
    url(r'^' + settings.MAIN_URL + r'admin/', admin.site.urls),
]

urlpatterns += [
    url(
        r'^' + settings.MAIN_URL + r'archive-autocomplete/$',
        ArchiveAutocomplete.as_view(),
        name='archive-autocomplete',
    ),
    url(
        r'^' + settings.MAIN_URL + r'gallery-autocomplete/$',
        GalleryAutocomplete.as_view(),
        name='gallery-autocomplete',
    ),
    url(
        r'^' + settings.MAIN_URL + r'gallery-select-autocomplete/$',
        GallerySelectAutocomplete.as_view(),
        name='gallery-select-autocomplete',
    ),
    url(
        r'^' + settings.MAIN_URL + r'tag-autocomplete/$',
        TagAutocomplete.as_view(),
        name='tag-autocomplete',
    ),
    url(
        r'^' + settings.MAIN_URL + r'tag-pk-autocomplete/$',
        TagPkAutocomplete.as_view(),
        name='tag-pk-autocomplete',
    ),
    url(
        r'^' + settings.MAIN_URL + r'noncustomtag-autocomplete/$',
        NonCustomTagAutocomplete.as_view(),
        name='noncustomtag-autocomplete',
    ),
    url(
        r'^' + settings.MAIN_URL + r'customtag-autocomplete/$',
        CustomTagAutocomplete.as_view(create_field='name'),
        name='customtag-autocomplete',
    ),
    url(
        r'^' + settings.MAIN_URL + r'wanted-gallery-autocomplete/$',
        WantedGalleryAutocomplete.as_view(),
        name='wanted-gallery-autocomplete',
    ),
    url(
        r'^' + settings.MAIN_URL + r'source-autocomplete/$',
        SourceAutocomplete.as_view(),
        name='source-autocomplete',
    ),
    url(
        r'^' + settings.MAIN_URL + r'provider-autocomplete/$',
        ProviderAutocomplete.as_view(),
        name='provider-autocomplete',
    ),
    url(
        r'^' + settings.MAIN_URL + r'reason-autocomplete/$',
        ReasonAutocomplete.as_view(),
        name='reason-autocomplete',
    ),
    url(
        r'^' + settings.MAIN_URL + r'uploader-autocomplete/$',
        UploaderAutocomplete.as_view(),
        name='uploader-autocomplete',
    ),
    url(
        r'^' + settings.MAIN_URL + r'category-autocomplete/$',
        CategoryAutocomplete.as_view(),
        name='category-autocomplete',
    ),
]

if settings.DEBUG:
    try:
        import debug_toolbar
        urlpatterns += [
            url(r'^' + settings.MAIN_URL + r'__debug__/', include(debug_toolbar.urls)),
        ]
        urlpatterns += static(settings.STATIC_URL,
                              document_root=settings.STATIC_ROOT)
        urlpatterns += static(settings.MEDIA_URL,
                              document_root=settings.MEDIA_ROOT)
    except ImportError:
        debug_toolbar = None

urlpatterns += [
    url(r'^' + settings.MAIN_URL, include('viewer.urls')),
]
