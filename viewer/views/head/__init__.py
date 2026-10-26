# Re-export all head views and filter functions for full backward compatibility
from viewer.views.head.common import (
    archive_filter_keys,
    archive_order_fields,
    archive_public_order_fields,
    archive_public_sort_by_fields,
    archive_sort_by_fields,
    crawler_settings,
    gallery_filter_keys,
    gallery_list_filters_keys,
    gallery_order_fields,
    wanted_gallery_filter_keys,
    wanted_gallery_order_fields,
)
from viewer.views.head.auth import (
    about,
    change_password,
    change_profile,
    render_error,
    render_message,
    session_settings,
    user_archive_preferences,
    viewer_login,
    viewer_logout,
)
from viewer.views.head.gallery import (
    filter_galleries,
    filter_galleries_simple,
    gallery_details,
    gallery_details_gid_provider,
    gallery_enter_reason,
    gallery_list,
    gallery_thumb,
    process_gallery_page,
    quick_gallery_search,
)
from viewer.views.head.search import (
    filter_archives,
    filter_archives_simple,
    filter_wanted_galleries_simple,
    image_url,
    public_stats,
    quick_search,
    search,
)
from viewer.views.head.submission import (
    url_submit,
)

__all__ = [
    # Common / Constants
    "archive_filter_keys",
    "archive_order_fields",
    "archive_public_order_fields",
    "archive_public_sort_by_fields",
    "archive_sort_by_fields",
    "crawler_settings",
    "gallery_filter_keys",
    "gallery_list_filters_keys",
    "gallery_order_fields",
    "wanted_gallery_filter_keys",
    "wanted_gallery_order_fields",
    # Auth
    "about",
    "change_password",
    "change_profile",
    "render_error",
    "render_message",
    "session_settings",
    "user_archive_preferences",
    "viewer_login",
    "viewer_logout",
    # Gallery
    "filter_galleries",
    "filter_galleries_simple",
    "gallery_details",
    "gallery_details_gid_provider",
    "gallery_enter_reason",
    "gallery_list",
    "gallery_thumb",
    "process_gallery_page",
    "quick_gallery_search",
    # Search
    "filter_archives",
    "filter_archives_simple",
    "filter_wanted_galleries_simple",
    "image_url",
    "public_stats",
    "quick_search",
    "search",
    # Submission
    "url_submit",
]
