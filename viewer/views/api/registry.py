"""Handler registries for the public JSON API (/api).

Each list maps a query-string command key to its handler. See dispatch.py for routing.
"""

from viewer.views.api.handlers.delete import (
    handle_delete_archive_group,
    handle_delete_archive_group_entry,
)
from viewer.views.api.handlers.get import (
    handle_get_archive,
    handle_get_archives,
    handle_get_at,
    handle_get_ah,
    handle_get_aof,
    handle_get_aid,
    handle_get_gallery,
    handle_get_gids,
    handle_get_gid,
    handle_get_gt,
    handle_get_sha1,
    handle_get_qa,
    handle_get_q,
    handle_get_match,
    handle_get_g,
    handle_get_gc,
    handle_get_gs,
    handle_get_gsp,
    handle_get_gd,
    handle_get_as,
    handle_get_archive_group,
    handle_get_archive_group_entry,
    handle_get_archive_group_entry_archive,
    handle_get_archive_wanted_image,
    handle_get_wanted_gallery,
    handle_get_wanted_galleries,
    handle_get_wanted_galleries_search,
)
from viewer.views.api.handlers.post import (
    handle_post_archive_group,
    handle_post_archive_group_entry,
    handle_post_wanted_gallery,
)
from viewer.views.api.handlers.put import (
    handle_put_archive_group,
    handle_put_archive_group_entry,
)

GET_HANDLERS = [
    ("archive", handle_get_archive),
    ("archives", handle_get_archives),
    ("at", handle_get_at),
    ("ah", handle_get_ah),
    ("aof", handle_get_aof),
    ("aid", handle_get_aid),
    ("gallery", handle_get_gallery),
    ("gids", handle_get_gids),
    ("gid", handle_get_gid),
    ("gt", handle_get_gt),
    ("sha1", handle_get_sha1),
    ("qa", handle_get_qa),
    ("q", handle_get_q),
    ("match", handle_get_match),
    ("g", handle_get_g),
    ("gc", handle_get_gc),
    ("gs", handle_get_gs),
    ("gsp", handle_get_gsp),
    ("gd", handle_get_gd),
    ("as", handle_get_as),
    ("archive-group", handle_get_archive_group),
    ("archive-group-entry", handle_get_archive_group_entry),
    ("archive-group-entry-archive", handle_get_archive_group_entry_archive),
    ("archive-wanted-image", handle_get_archive_wanted_image),
    ("wanted-gallery", handle_get_wanted_gallery),
    ("wanted-galleries", handle_get_wanted_galleries),
    ("wanted-galleries-search", handle_get_wanted_galleries_search),
]


POST_HANDLERS = [
    ("archive-group", "viewer.change_archivegroup", handle_post_archive_group),
    ("archive-group-entry", "viewer.change_archivegroupentry", handle_post_archive_group_entry),
    ("wanted-gallery", "viewer.add_wantedgallery", handle_post_wanted_gallery),
]


PUT_HANDLERS = [
    ("archive-group", "viewer.change_archivegroup", handle_put_archive_group),
    ("archive-group-entry", "viewer.change_archivegroupentry", handle_put_archive_group_entry),
]


DELETE_HANDLERS = [
    ("archive-group", "viewer.delete_archivegroup", handle_delete_archive_group),
    ("archive-group-entry", "viewer.delete_archivegroupentry", handle_delete_archive_group_entry),
]
