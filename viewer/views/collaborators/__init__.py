# Re-export all collaborator views for full backward compatibility
from viewer.views.manager import wanted_galleries
from viewer.views.wanted import wanted_gallery
from viewer.views.collaborators.archive_manager import (
    archive_manager,
    archives_not_matched_with_gallery,
    archives_similar_by_fields,
    filter_by_extra_files,
    filter_by_marks,
    manage_archives,
)
from viewer.views.collaborators.batch_operations import (
    archive_update,
    gallery_match_groups_possibles,
    gallery_update,
    manage_galleries,
    missing_archives_for_galleries,
    repeated_galleries_by_field,
)
from viewer.views.collaborators.upload import (
    submit_queue,
    upload_archive,
    upload_gallery,
    user_crawler,
)
from viewer.views.collaborators.history import (
    activity_event_log,
    download_history,
    my_event_log,
)
from viewer.views.collaborators.links import (
    _can_manage_monitored_links,
    create_monitored_link,
    create_wanted_galleries_from_command,
    create_wanted_gallery,
    delete_monitored_link,
    edit_monitored_link,
    monitored_link_action,
    monitored_links,
    user_token,
)

__all__ = [
    # Wanted re-exports
    "wanted_galleries",
    "wanted_gallery",
    # Archive Manager
    "archive_manager",
    "archives_not_matched_with_gallery",
    "archives_similar_by_fields",
    "filter_by_extra_files",
    "filter_by_marks",
    "manage_archives",
    # Batch Operations
    "archive_update",
    "gallery_match_groups_possibles",
    "gallery_update",
    "manage_galleries",
    "missing_archives_for_galleries",
    "repeated_galleries_by_field",
    # Upload
    "submit_queue",
    "upload_archive",
    "upload_gallery",
    "user_crawler",
    # History
    "activity_event_log",
    "download_history",
    "my_event_log",
    # Links
    "_can_manage_monitored_links",
    "create_monitored_link",
    "create_wanted_galleries_from_command",
    "create_wanted_gallery",
    "delete_monitored_link",
    "edit_monitored_link",
    "monitored_link_action",
    "monitored_links",
    "user_token",
]
