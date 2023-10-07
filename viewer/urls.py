from django.urls import path
from django.urls import re_path
from django.conf import settings

from viewer.views import head, browser, wanted, exp, api, archive, \
    admin, manager, collaborators, groups, admin_api, tools, elasticsearch, gallery
from viewer.feeds import LatestArchivesFeed
from viewer.views.elasticsearch import ESHomePageView, ESHomeGalleryPageView, autocomplete_view, \
    ESArchiveJSONView, ESGalleryJSONView

app_name = 'viewer'

urlpatterns = [

    re_path(r"^dir-browser/$", browser.directory_parser, name='directory-parser'),
    re_path(r"^api-login/*$", api.api_login, name='api-login'),
    re_path(r"^api-logout/*$", api.api_logout, name='api-logout'),
    re_path(r"^jsearch/*$", api.json_search, name='json-search'),
    re_path(r"^api/*$", api.json_search, name='api'),
    re_path(r"^jsonapi$", api.json_parser, name='json-parser'),
    re_path(r"^admin-api/$", api.json_parser, name='json-parser'),
    re_path(r"^archive/(\d+)/$", archive.archive_details, name='archive'),
    re_path(r"^archive/(\d+)/(edit)/$", archive.archive_details, name='archive-edit'),
    re_path(r"^archive/(\d+)/extract-toggle/$", archive.extract_toggle, name='archive-extract-toggle'),
    re_path(r"^archive/(\d+)/extract/$", archive.extract, name='archive-extract'),
    re_path(r"^archive/(\d+)/reduce/$", archive.reduce, name='archive-reduce'),
    re_path(r"^archive/(\d+)/public/$", archive.public, name='archive-public'),
    re_path(r"^archive/(\d+)/private/$", archive.private, name='archive-private'),
    re_path(r"^archive/(\d+)/calc-imgs-sha1/$", archive.calculate_images_sha1, name='archive-calc-imgs-sha1'),
    re_path(r"^archive/(\d+)/check-convert-type/$", archive.check_and_convert_filetype, name='check-convert-type'),
    re_path(r"^archive/(\d+)/clone-plus/$", archive.clone_plus, name='archive-clone-plus'),
    re_path(r"^archive/(\d+)/recalc-info/$", archive.recalc_info, name='archive-recalc-info'),
    re_path(r"^archive/(\d+)/mark-similar-archives/$", archive.mark_similar_archives, name='archive-mark-similar'),
    re_path(r"^archive/(\d+)/recall-api/$", archive.recall_api, name='archive-recall-api'),
    re_path(r"^archive/(\d+)/generate-matches/$", archive.generate_matches, name='archive-generate-matches'),
    re_path(r"^archive/(\d+)/rematch/$", archive.rematch_archive, name='archive-rematch'),
    re_path(r"^archive/(\d+)/delete/$", archive.delete_archive, name='archive-delete'),
    re_path(r"^archive/(\d+)/thumb/$", archive.archive_thumb, name='archive-thumb'),
    re_path(r"^archive/(\d+)/image-data-list/$", archive.image_data_list, name='image-data-list'),
    re_path(r"^archive/(\d+)/change-log/$", archive.change_log, name='archive-change-log'),
    re_path(r"^archive-manage/(\d+)/delete/$", archive.delete_manage_archive, name='archive-manage-delete'),
    re_path(r"^update/(\d+)/([\w-]+)/(\d*)/$", archive.archive_update, name='archive-update-tool-id'),
    re_path(r"^update/(\d+)/([\w-]+)/$", archive.archive_update, name='archive-update-tool'),
    re_path(r"^update/(\d+)/([\w-]+)/([\w-]+)/$", archive.archive_update, name='archive-update-tool-name'),
    re_path(r"^live-image-thumbnail/(\d+)/(\d+)/$", archive.image_live_thumb, name='archive-live-image'),
    re_path(r"^update/(\d+)/$", archive.archive_update, name='archive-update'),
    re_path(r"^archive-tool-reason/(\d+)/([\w-]+)/$", archive.archive_enter_reason, name='archive-tool-reason'),
    re_path(r"^gallery/(\d+)/thumb/$", head.gallery_thumb, name='gallery-thumb'),
    re_path(r"^gallery/(\d+)/change-log/$", gallery.change_log, name='gallery-change-log'),
    re_path(r"^gallery/(\d+)/([\w-]+)/$", head.gallery_details, name='gallery-tool'),
    re_path(r"^gallery-tool-reason/(\d+)/([\w-]+)/$", head.gallery_enter_reason, name='gallery-tool-reason'),
    re_path(r"^gallery/(\d+)/$", head.gallery_details, name='gallery'),
    re_path(r"^wanted-gallery/(\d+)/$", wanted.wanted_gallery, name='wanted-gallery'),
    re_path(r"^user-archive-preferences/(\d+)/([\w-]+)/$", head.user_archive_preferences, name='user-archive-preferences'),
    re_path(r"^submit/$", head.url_submit, name='url-submit'),
    re_path(r"^login/$", head.viewer_login, name='login'),
    re_path(r"^logout/$", head.viewer_logout, name='logout'),
    re_path(r"^password/$", head.change_password, name='change-password'),
    re_path(r"^profile/$", head.change_profile, name='change-profile'),
    re_path(r"^session-settings/$", head.session_settings, name='session-settings'),
    re_path(r"^img/(\d+)/$", head.image_url, name='image-url'),
    re_path(r"^content/panda.user.js$", head.panda_userscript, name='panda-user-script'),
    re_path(r"^about/$", head.about, name='about'),
    re_path(r"^archive-auth/$", archive.archive_auth, name='archive-auth'),
]

# For now, we don't have an ES page for direct tag search (Archive/Gallery). It defaults to regular page.
if settings.CRAWLER_SETTINGS.urls.elasticsearch_as_main_urls and settings.CRAWLER_SETTINGS.elasticsearch.enable:
    urlpatterns += [
        re_path(r"^(gallery-tag)/(.+?)/$", ESHomeGalleryPageView.as_view(), name='gallery-tag-search'),
        re_path(r'^galleries/$', ESHomeGalleryPageView.as_view(), name='gallery-list'),
        re_path(r'^(tag)/(.+?)/$', ESHomePageView.as_view(), name='archive-tag-search'),
        re_path(r'^search/$', ESHomePageView.as_view(), name='archive-search'),
        re_path(r"^$", ESHomePageView.as_view(), name="main-page"),
    ]
else:
    urlpatterns += [
        re_path(r"^(gallery-tag)/(.+?)/$", head.gallery_list, name='gallery-tag-search'),
        re_path(r"^galleries/$", head.gallery_list, name='gallery-list'),
        re_path(r"^(tag)/(.+?)/$", head.search, name='archive-tag-search'),
        re_path(r"^search/$", head.search, name='archive-search'),
        re_path(r"^$", head.search, name="main-page"),
    ]

if settings.CRAWLER_SETTINGS.urls.external_as_main_download and settings.CRAWLER_SETTINGS.urls.external_media_server:
    urlpatterns += [
        re_path(r"^archive/(\d+)/download/$", archive.archive_ext_download, name='archive-download'),
        re_path(r"^archive/(\d+)/ext-download/$", archive.archive_download, name='archive-ext-download'),
    ]
else:
    urlpatterns += [
        re_path(r"^archive/(\d+)/download/$", archive.archive_download, name='archive-download'),
        re_path(r"^archive/(\d+)/ext-download/$", archive.archive_ext_download, name='archive-ext-download'),
    ]

# Manager lists.
urlpatterns += [
    re_path(r"^repeated-archives/$", manager.repeated_archives_for_galleries, name='repeated-archives'),
    re_path(r"^galleries-by-field/$", manager.repeated_galleries_by_field, name='galleries-by-field'),
    re_path(r"^archive-filesize-different/$", manager.archive_filesize_different_from_gallery, name='archive-filesize-different'),
    re_path(r"^missing-archives/$", manager.public_missing_archives_for_galleries, name='public-missing-archives'),
    re_path(r"^archive-not-present/$", manager.archives_not_present_in_filesystem, name='archive-not-present'),
    re_path(r"^archives-not-matched/$", manager.archives_not_matched_with_gallery, name='archives-not-matched'),
    re_path(r"^wanted-galleries/$", manager.wanted_galleries, name='wanted-galleries'),
    re_path(r"^found-galleries/$", manager.found_galleries, name='found-galleries'),
]

# Image viewer.
urlpatterns += [
    re_path(r"^new-image-viewer/(\d+)/img/(\d+)/$", exp.new_image_viewer, name='new-image-viewer'),
]

# Admin URLs
urlpatterns += [
    re_path(r"^tools/([\w-]+)/$", admin.tools, name='tools-id'),
    re_path(r"^tools/([\w-]+)/([\w-]+)/$", admin.tools, name='tools-id-arg'),
    re_path(r"^tools/$", admin.tools, name='tools'),
    re_path(r"^logs/$", admin.logs, name='logs'),
    re_path(r"^stats/collection$", admin.stats_collection, name='stats-collection'),
    re_path(r"^public-stats/$", head.public_stats, name='public-stats'),
    re_path(r"^stats/workers$", admin.stats_workers, name='stats-workers'),
    re_path(r"^stats/settings", admin.stats_settings, name='stats-settings'),
    re_path(r"^web-queue/([\w-]+)/([\w-]+)$", admin.queue_operations, name='queue-operations'),
    re_path(r"^web-crawler/$", admin.crawler, name='crawler'),
    re_path(r"^folder-crawler/$", admin.foldercrawler, name='folder-crawler'),
    re_path(r"^tools-api/([\w-]+)/$", admin_api.tools, name='tools-api'),
    re_path(r"^tools-api/([\w-]+)/([\w-]+)/$", admin_api.tools, name='tools-api-id-arg'),
]

# Collaborators.
urlpatterns += [
    re_path(r"^my-event-log/$", collaborators.my_event_log, name='my-event-log'),
    re_path(r"^submit-queue/$", collaborators.submit_queue, name='submit-queue'),
    re_path(r"^upload-archive/$", collaborators.upload_archive, name='upload-archive'),
    re_path(r"^upload-gallery/$", collaborators.upload_gallery, name='upload-gallery'),
    re_path(r"^manage-archives/$", collaborators.manage_archives, name='manage-archives'),
    re_path(r"^col-wanted-galleries/$", collaborators.wanted_galleries, name='col-wanted-galleries'),
    re_path(r"^col-wanted-gallery/(\d+)/$", collaborators.wanted_gallery, name='col-wanted-gallery'),
    re_path(r"^col-create-wanted-gallery/$", collaborators.create_wanted_gallery, name='col-create-wanted-gallery'),
    re_path(r"^user-crawler/$", collaborators.user_crawler, name='user-crawler'),
    re_path(r"^match-archives/$", collaborators.archives_not_matched_with_gallery, name='match-archives'),
    re_path(r"^col-update/(\d+)/([\w-]+)/(\d*)/$", collaborators.archive_update, name='col-archive-update-tool-id'),
    re_path(r"^col-update/(\d+)/([\w-]+)/$", collaborators.archive_update, name='col-archive-update-tool'),
    re_path(r"^col-update/(\d+)/([\w-]+)/([\w-]+)/$", collaborators.archive_update, name='col-archive-update-tool-name'),
    re_path(r"^col-update/(\d+)/$", collaborators.archive_update, name='col-archive-update'),
    re_path(r"^col-missing-archives/$", collaborators.missing_archives_for_galleries, name='col-missing-archives'),
    re_path(r"^activity-event-log/$", collaborators.activity_event_log, name='activity-event-log'),
    re_path(r"^monitored-links$", collaborators.monitored_links, name='monitored-links'),
    re_path(r"^archives-by-field/$", collaborators.archives_similar_by_fields, name='archives-by-field'),
    re_path(r"^user-token/([\w-]+)/$", collaborators.user_token, name='user-token'),
]

# Archive groups.
urlpatterns += [
    re_path(r"^archive-groups/$", groups.archive_groups_explorer, name='archive-groups'),
    path("archive-group/<slug:slug>/", groups.archive_group, name='archive-group'),
    path("archive-group/<int:pk>/", groups.archive_group, name='archive-group'),
    path("archive-group-edit/<slug:slug>/", groups.archive_group_edit, name='archive-group-edit'),
    path("archive-group-edit/<int:pk>/", groups.archive_group_edit, name='archive-group-edit'),
]

urlpatterns += [
    re_path(r"^feed/$", LatestArchivesFeed(), name='archive-rss'),
]

urlpatterns += [
    re_path(r"^r-api/([\w-]+)/$", exp.api, name='model-all'),
    re_path(r"^r-api/([\w-]+)/(\d+)/$", exp.api, name='model-obj'),
    re_path(r"^r-api/([\w-]+)/(\d+)/([\w-]+)/$", exp.api, name='model-obj-action'),
]

urlpatterns += [
    re_path(r'^autocomplete-view/$', autocomplete_view, name='es-autocomplete-view'),
    re_path(r'^title-text-suggest/$', elasticsearch.title_suggest_view, name='es-title-text-suggest-view'),
    re_path(r'^title-suggest/$', elasticsearch.title_suggest_archive_view, name='es-suggest-view'),
    re_path(r'^es-title-pk-suggest/$', elasticsearch.title_pk_suggest_archive_view, name='es-title-pk-suggest'),
    re_path(r'^es-archive-simple/$', elasticsearch.archive_simple, name='es-archive-simple'),
    re_path(r'^es-archives-simple/$', elasticsearch.archives_simple, name='es-archives-simple'),
    re_path(r'^es-index/$', ESHomePageView.as_view(), name='es-index-view'),
    re_path(r'^es-gallery-index/$', ESHomeGalleryPageView.as_view(), name='es-gallery-index-view'),
    re_path(r'^es-archive-json/$', ESArchiveJSONView.as_view(), name='es-archive-json'),
    re_path(r'^es-gallery-json/$', ESGalleryJSONView.as_view(), name='es-gallery-json'),
]

urlpatterns += [
    re_path(r'^compare-archives/$', tools.compare_archives, name='compare-archives'),
    re_path(r"^compare-archives-viewer/$", tools.compare_archives_viewer, name='compare-archives-viewer'),
]
