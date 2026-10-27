from django.conf import settings
from django.urls import path, register_converter, re_path

from viewer.views import (
    head,
    browser,
    wanted,
    exp,
    api,
    archive,
    admin,
    manager,
    collaborators,
    archive_groups,
    admin_api,
    tools,
    elasticsearch,
    gallery,
    gallery_match_groups,
    stats,
    image_search,
    workers_dashboard,
)

from viewer.feeds import LatestArchivesFeed
from viewer.views.elasticsearch import (
    ESHomePageView,
    ESHomeGalleryPageView,
    autocomplete_view,
    ESArchiveJSONView,
    ESGalleryJSONView,
)

app_name = "viewer"


class EditModeConverter:
    regex = "edit"

    def to_python(self, value: str) -> str:
        return value

    def to_url(self, value: str) -> str:
        return value


class TagModeConverter:
    regex = "tag"

    def to_python(self, value: str) -> str:
        return value

    def to_url(self, value: str) -> str:
        return value


class GalleryTagModeConverter:
    regex = "gallery-tag"

    def to_python(self, value: str) -> str:
        return value

    def to_url(self, value: str) -> str:
        return value


register_converter(EditModeConverter, "edit_mode")
register_converter(TagModeConverter, "tag_mode")
register_converter(GalleryTagModeConverter, "gallery_tag_mode")

urlpatterns = [
    path("dir-browser/", browser.directory_parser, name="directory-parser"),
    path("api-login/", api.api_login, name="api-login"),
    path("api-logout/", api.api_logout, name="api-logout"),
    path("jsearch/", api.json_api, name="json-search"),
    path("api/", api.json_api, name="api"),
    path("jsonapi/", api.json_parser, name="jsonapi"),
    re_path(r"^jsonapi$", api.json_parser, name="jsonapi-slashless"),
    path("admin-api/", api.json_parser, name="json-parser"),
    path("archive/<int:pk>/", archive.archive_details, name="archive"),
    path("archive/<int:pk>/<edit_mode:mode>/", archive.archive_details, name="archive-edit"),
    path("archive/<int:pk>/extract-toggle/", archive.extract_toggle, name="archive-extract-toggle"),
    path("archive/<int:pk>/extract/", archive.extract, name="archive-extract"),
    path("archive/<int:pk>/reduce/", archive.reduce, name="archive-reduce"),
    path("archive/<int:pk>/public/", archive.public, name="archive-public"),
    path("archive/<int:pk>/private/", archive.private, name="archive-private"),
    path("archive/<int:pk>/calc-imgs-sha1/", archive.calculate_images_sha1, name="archive-calc-imgs-sha1"),
    path("archive/<int:pk>/check-convert-type/", archive.check_and_convert_filetype, name="check-convert-type"),
    path("archive/<int:pk>/clone-plus/", archive.clone_plus, name="archive-clone-plus"),
    path("archive/<int:pk>/split/", archive.split, name="archive-split"),
    path("archive/<int:pk>/recalc-info/", archive.recalc_info, name="archive-recalc-info"),
    path("archive/<int:pk>/mark-similar-archives/", archive.mark_similar_archives, name="archive-mark-similar"),
    path("archive/<int:pk>/recall-api/", archive.recall_api, name="archive-recall-api"),
    path("archive/<int:pk>/generate-matches/", archive.generate_matches, name="archive-generate-matches"),
    path("archive/<int:pk>/rematch/", archive.rematch_archive, name="archive-rematch"),
    path("archive/<int:pk>/delete/", archive.delete_archive, name="archive-delete"),
    path("archive/<int:pk>/thumb/", archive.archive_thumb, name="archive-thumb"),
    path("archive/<int:pk>/image-data-list/", archive.image_data_list, name="image-data-list"),
    path("archive/<int:pk>/change-log/", archive.change_log, name="archive-change-log"),
    path("archive-manage/<int:pk>/delete/", archive.delete_manage_archive, name="archive-manage-delete"),
    path(
        "archive-manage/<int:pk>/remove-from-index/",
        archive.remove_from_index_manage_archive,
        name="archive-manage-remove-from-index",
    ),
    path(
        "archive-manage/<int:pk>/add-to-index/",
        archive.add_to_index_manage_archive,
        name="archive-manage-add-to-index",
    ),
    path("update/<int:pk>/<str:tool>/<int:tool_use_id>/", archive.archive_update, name="archive-update-tool-id"),
    path("update/<int:pk>/<str:tool>/<str:tool_use_id>/", archive.archive_update, name="archive-update-tool-name"),
    path("update/<int:pk>/<str:tool>/", archive.archive_update, name="archive-update-tool"),
    path("live-image-thumbnail/<int:archive_pk>/<int:position>/", archive.image_live_thumb, name="archive-live-image"),
    path("update/<int:pk>/", archive.archive_update, name="archive-update"),
    path("archive-tool-reason/<int:pk>/<str:tool>/", archive.archive_enter_reason, name="archive-tool-reason"),
    path("gallery/<int:pk>/thumb/", head.gallery_thumb, name="gallery-thumb"),
    path("gallery/<int:pk>/change-log/", gallery.change_log, name="gallery-change-log"),
    path("gallery-tool-reason/<int:pk>/<str:tool>/", head.gallery_enter_reason, name="gallery-tool-reason"),
    path("gallery/<int:pk>/<str:tool>/", head.gallery_details, name="gallery-tool"),
    path("gallery/<int:pk>/", head.gallery_details, name="gallery"),
    path("gallery-find/<str:tool>/", head.gallery_details_gid_provider, name="gallery-find-tool"),
    path("gallery-find/", head.gallery_details_gid_provider, name="gallery-find"),
    path("wanted-gallery/<int:pk>/", wanted.wanted_gallery, name="wanted-gallery"),
    path(
        "user-archive-preferences/<int:archive_pk>/<str:setting>/",
        head.user_archive_preferences,
        name="user-archive-preferences",
    ),
    path("submit/", head.url_submit, name="url-submit"),
    path("login/", head.viewer_login, name="login"),
    path("logout/", head.viewer_logout, name="logout"),
    path("password/", head.change_password, name="change-password"),
    path("profile/", head.change_profile, name="change-profile"),
    path("session-settings/", head.session_settings, name="session-settings"),
    path("img/<int:pk>/", head.image_url, name="image-url"),
    path("about/", head.about, name="about"),
    path("archive-auth/", archive.archive_auth, name="archive-auth"),
]

# For now, we don't have an ES page for direct tag search (Archive/Gallery). It defaults to regular page.
if settings.CRAWLER_SETTINGS.urls.elasticsearch_as_main_urls and settings.CRAWLER_SETTINGS.elasticsearch.enable:
    urlpatterns += [
        path("<gallery_tag_mode:mode>/<path:tag>/", ESHomeGalleryPageView.as_view(), name="gallery-tag-search"),
        path("galleries/", ESHomeGalleryPageView.as_view(), name="gallery-list"),
        path("<tag_mode:mode>/<path:tag>/", ESHomePageView.as_view(), name="archive-tag-search"),
        path("search/", ESHomePageView.as_view(), name="archive-search"),
        path("", ESHomePageView.as_view(), name="main-page"),
    ]
else:
    urlpatterns += [
        path("<gallery_tag_mode:mode>/<path:tag>/", head.gallery_list, name="gallery-tag-search"),
        path("galleries/", head.gallery_list, name="gallery-list"),
        path("<tag_mode:mode>/<path:tag>/", head.search, name="archive-tag-search"),
        path("search/", head.search, name="archive-search"),
        path("", head.search, name="main-page"),
    ]

if settings.CRAWLER_SETTINGS.urls.external_as_main_download and settings.CRAWLER_SETTINGS.urls.external_media_server:
    urlpatterns += [
        path("archive/<int:pk>/download/", archive.archive_ext_download, name="archive-download"),
        path("archive/<int:pk>/ext-download/", archive.archive_download, name="archive-ext-download"),
    ]
else:
    urlpatterns += [
        path("archive/<int:pk>/download/", archive.archive_download, name="archive-download"),
        path("archive/<int:pk>/ext-download/", archive.archive_ext_download, name="archive-ext-download"),
    ]

# Manager lists.
urlpatterns += [
    path("repeated-archives/", manager.repeated_archives_for_galleries, name="repeated-archives"),
    # path("galleries-by-field/", manager.repeated_galleries_by_field, name="galleries-by-field"),
    path(
        "archive-filesize-different/",
        manager.archive_filesize_different_from_gallery,
        name="archive-filesize-different",
    ),
    path("missing-archives/", manager.public_missing_archives_for_galleries, name="public-missing-archives"),
    path("archive-not-present/", manager.archives_not_present_in_filesystem, name="archive-not-present"),
    path("archives-not-matched/", manager.archives_not_matched_with_gallery, name="archives-not-matched"),
    path("wanted-galleries/", manager.wanted_galleries, name="wanted-galleries"),
    path("found-galleries/", manager.found_galleries, name="found-galleries"),
]

# Image viewer.
urlpatterns += [
    path("new-image-viewer/<int:archive>/img/<int:image>/", exp.new_image_viewer, name="new-image-viewer"),
]

# Admin URLs
urlpatterns += [
    path("tools/<str:tool>/<str:tool_arg>/", admin.tools, name="tools-id-arg"),
    path("tools/<str:tool>/", admin.tools, name="tools-id"),
    path("tools/", admin.tools, name="tools"),
    path("logs/", admin.logs, name="logs"),
    path("stats/collection/", admin.stats_collection, name="stats-collection"),
    path("public-stats/", head.public_stats, name="public-stats"),
    path("stats/workers/", admin.stats_workers, name="stats-workers"),
    re_path(r"^stats/workers$", admin.stats_workers),
    path("stats/settings/", admin.stats_settings, name="stats-settings"),
    path("web-queue/<str:operation>/<str:arguments>/", admin.queue_operations, name="queue-operations"),
    re_path(r"^web-queue/(?P<operation>[\w-]+)/(?P<arguments>[\w-]+)$", admin.queue_operations),
    path("web-crawler/", admin.crawler, name="crawler"),
    path("folder-crawler/", admin.foldercrawler, name="folder-crawler"),
    path("tools-api/<str:tool>/<str:tool_arg>/", admin_api.tools, name="tools-api-id-arg"),
    path("tools-api/<str:tool>/", admin_api.tools, name="tools-api"),
    path("stats/", stats.stats_view, name="stats-visualizations"),
    path("api/stats/", stats.stats_api, name="api-stats"),
    path("api/workers/status/", workers_dashboard.workers_status_api, name="api-workers-status"),
    path("api/workers/action/", workers_dashboard.workers_action_api, name="api-workers-action"),
]

# Collaborators.
urlpatterns += [
    path("my-event-log/", collaborators.my_event_log, name="my-event-log"),
    path("submit-queue/", collaborators.submit_queue, name="submit-queue"),
    path("upload-archive/", collaborators.upload_archive, name="upload-archive"),
    path("upload-gallery/", collaborators.upload_gallery, name="upload-gallery"),
    path("manage-archives/", collaborators.manage_archives, name="manage-archives"),
    path("archive-manager/", collaborators.archive_manager, name="archive-manager"),
    path("manage-galleries/", collaborators.manage_galleries, name="manage-galleries"),
    path(
        "gallery-match-groups-batch/",
        collaborators.gallery_match_groups_possibles,
        name="gallery-match-groups-batch",
    ),
    path("col-wanted-galleries/", collaborators.wanted_galleries, name="col-wanted-galleries"),
    path("col-wanted-gallery/<int:pk>/", collaborators.wanted_gallery, name="col-wanted-gallery"),
    path("col-create-wanted-gallery/", collaborators.create_wanted_gallery, name="col-create-wanted-gallery"),
    path(
        "col-create-wanted-galleries-from-command/",
        collaborators.create_wanted_galleries_from_command,
        name="col-create-wanted-galleries-from-command",
    ),
    path("user-crawler/", collaborators.user_crawler, name="user-crawler"),
    path("download-history/", collaborators.download_history, name="download-history"),
    path("download-monitor/", collaborators.download_history, name="download-monitor"),
    path("downloads/", collaborators.download_history, name="downloads"),
    path("match-archives/", collaborators.archives_not_matched_with_gallery, name="match-archives"),
    path(
        "col-update/<int:pk>/<str:tool>/<int:tool_use_id>/",
        collaborators.archive_update,
        name="col-archive-update-tool-id",
    ),
    path(
        "col-gallery-update/<int:pk>/<str:tool>/<int:tool_use_id>/",
        collaborators.gallery_update,
        name="col-gallery-update-tool-id",
    ),
    path("col-gallery-update/<int:pk>/<str:tool>/", collaborators.gallery_update, name="col-gallery-update-tool"),
    path(
        "col-update/<int:pk>/<str:tool>/<str:tool_use_id>/",
        collaborators.archive_update,
        name="col-archive-update-tool-name",
    ),
    path("col-update/<int:pk>/<str:tool>/", collaborators.archive_update, name="col-archive-update-tool"),
    path("col-update/<int:pk>/", collaborators.archive_update, name="col-archive-update"),
    path("col-missing-archives/", collaborators.missing_archives_for_galleries, name="col-missing-archives"),
    path("activity-event-log/", collaborators.activity_event_log, name="activity-event-log"),
    path("monitored-links/", collaborators.monitored_links, name="monitored-links"),
    path("monitored-link/create/", collaborators.create_monitored_link, name="col-create-monitored-link"),
    path("monitored-link/<int:pk>/edit/", collaborators.edit_monitored_link, name="col-edit-monitored-link"),
    path("monitored-link/<int:pk>/delete/", collaborators.delete_monitored_link, name="col-delete-monitored-link"),
    path("monitored-link/<int:pk>/<str:action>/", collaborators.monitored_link_action, name="col-monitored-link-action"),
    path("archives-by-field/", collaborators.archives_similar_by_fields, name="archives-by-field"),
    path("galleries-by-field/", collaborators.repeated_galleries_by_field, name="galleries-by-field"),
    path("user-token/<str:operation>/", collaborators.user_token, name="user-token"),
]

# Archive groups.
urlpatterns += [
    path("archive-groups/", archive_groups.archive_groups_explorer, name="archive-groups"),
    path("archive-group/<slug:slug>/", archive_groups.archive_group_details, name="archive-group"),
    path("archive-group/<int:pk>/", archive_groups.archive_group_details, name="archive-group"),
    path("archive-group-edit/<int:pk>/", archive_groups.archive_group_edit, name="archive-group-edit"),
    path("archive-group-edit/<slug:slug>/", archive_groups.archive_group_edit, name="archive-group-edit"),
    path(
        "archive-group-reason/<int:pk>/<str:tool>/",
        archive_groups.archive_group_enter_reason,
        name="archive-group-tool-reason",
    ),
    path(
        "archive-group-reason/<slug:slug>/<str:tool>/",
        archive_groups.archive_group_enter_reason,
        name="archive-group-tool-reason",
    ),
]

# Gallery match groups.
urlpatterns += [
    path("gallery-match-groups/", gallery_match_groups.gallery_match_groups_explorer, name="gallery-match-groups"),
    path("gallery-match-group/<int:pk>/", gallery_match_groups.gallery_match_group_details, name="gallery-match-group"),
    path(
        "gallery-match-group-edit/<int:pk>/",
        gallery_match_groups.gallery_match_group_edit,
        name="gallery-match-group-edit",
    ),
    path(
        "gallery-match-group-reason/<int:pk>/<str:tool>/",
        gallery_match_groups.gallery_match_group_enter_reason,
        name="gallery-match-group-tool-reason",
    ),
]

urlpatterns += [
    path("feed/", LatestArchivesFeed(), name="archive-rss"),
]

urlpatterns += [
    path("r-api/<str:model>/<int:obj_id>/<str:action>/", exp.api, name="model-obj-action"),
    path("r-api/<str:model>/<int:obj_id>/", exp.api, name="model-obj"),
    path("r-api/<str:model>/", exp.api, name="model-all"),
]

urlpatterns += [
    path("autocomplete-view/", autocomplete_view, name="es-autocomplete-view"),
    path("title-text-suggest/", elasticsearch.title_suggest_view, name="es-title-text-suggest-view"),
    path("title-suggest/", elasticsearch.title_suggest_archive_view, name="es-suggest-view"),
    path("es-title-pk-suggest/", elasticsearch.title_pk_suggest_archive_view, name="es-title-pk-suggest"),
    path("es-archive-simple/", elasticsearch.archive_simple, name="es-archive-simple"),
    path("es-archives-simple/", elasticsearch.archives_simple, name="es-archives-simple"),
    path(
        f"{settings.CRAWLER_SETTINGS.urls.elasticsearch_archive_url}/",
        ESHomePageView.as_view(),
        name="es-index-view",
    ),
    path(
        f"{settings.CRAWLER_SETTINGS.urls.elasticsearch_gallery_url}/",
        ESHomeGalleryPageView.as_view(),
        name="es-gallery-index-view",
    ),
    path("es-archive-json/", ESArchiveJSONView.as_view(), name="es-archive-json"),
    path("es-gallery-json/", ESGalleryJSONView.as_view(), name="es-gallery-json"),
]

urlpatterns += [
    path("compare-archives/", tools.compare_archives, name="compare-archives"),
    path("compare-archives-viewer/", tools.compare_archives_viewer, name="compare-archives-viewer"),
    path("archive-group-editor/", tools.archive_group_editor, name="archive-group-editor"),
    path("image-search/", image_search.image_search, name="image-search"),
]
