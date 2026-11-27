"""Command registry for the JSON API (``/api/v1/``, and the legacy ``/api/`` and ``/jsearch/``).

A request is routed to the first command of its HTTP method whose key is in the query string (see dispatch.py), so
the order inside each method matters when a request carries several command keys. Each command also describes its
parameters, permission and body, which is what the OpenAPI document (openapi.py) is generated from.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from django.http import HttpResponse

from viewer.views.api.handlers.delete import (
    handle_delete_archive,
    handle_delete_archive_group,
    handle_delete_archive_group_entry,
    handle_delete_gallery,
    handle_delete_gid,
    handle_delete_wanted_gallery,
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
    handle_get_wanted_tags,
)
from viewer.views.api.handlers.post import (
    handle_post_archive_group,
    handle_post_archive_group_entry,
    handle_post_update_metadata,
    handle_post_wanted_gallery,
)
from viewer.views.api.handlers.put import (
    handle_put_archive_group,
    handle_put_archive_group_entry,
    handle_put_gallery,
    handle_put_gid,
    handle_put_wanted_gallery,
)
from viewer.views.api.handlers.reader import (
    handle_get_archive_images,
    handle_get_archive_reader,
    handle_get_archives_browse,
    handle_get_archives_quick,
    handle_get_archives_simple,
    handle_get_me,
    handle_post_extract_archive,
    handle_post_reduce_archive,
)
from viewer.views.api.handlers.tasks import handle_get_task, handle_get_tasks
from viewer.views.head import archive_filter_keys, gallery_filter_keys, wanted_gallery_filter_keys


@dataclass(frozen=True)
class Param:
    """A query string parameter, as documented in the OpenAPI document."""

    name: str
    description: str = ""
    type: str = "string"
    required: bool = False
    multi: bool = False
    example: Any = None


@dataclass(frozen=True)
class Command:
    method: str
    key: str
    handler: Callable[..., HttpResponse]
    summary: str
    tag: str
    # The command key's own value. None documents a flag whose value is ignored (``?gsp`` or ``?gsp=``).
    key_param: Param | None = None
    params: tuple[Param, ...] = ()
    # Django permission(s) needed; with several, any one of them is enough.
    perm: str | tuple[str, ...] | None = None
    # GET commands are open to anonymous callers (who only see public data) unless this is set.
    auth_required: bool = False
    description: str = ""
    # JSON schema of the request body (POST/PUT/DELETE).
    body: dict[str, Any] | None = None
    # JSON schema of a successful response; a generic object/array when not given.
    response: dict[str, Any] | None = None
    # The command queues background worker tasks and answers with task_id(s) and status_url.
    queues_tasks: bool = False

    @property
    def requires_auth(self) -> bool:
        return self.auth_required or self.method != "GET"


def _id(key: str, what: str, multi: bool = False) -> Param:
    return Param(key, f"{what} ID{'s (repeat the parameter)' if multi else ''}.", "integer", True, multi)


def _filters(keys: Sequence[str], sortable: bool = True) -> tuple[Param, ...]:
    params = [Param(key, "Filter, same as the search page.") for key in keys if key != "tags"]
    params.append(
        Param("tags", "Comma-separated tags (at most 100 per request); prefix with ^ for exact, - to exclude, -^ for both.")
    )
    if sortable:
        params.append(Param("sort", "Sort field."))
        params.append(Param("asc_desc", "asc or desc."))
    return tuple(params)


GALLERY_FILTERS = _filters(gallery_filter_keys)
ARCHIVE_FILTERS = _filters(archive_filter_keys)
WANTED_FILTERS = _filters(wanted_gallery_filter_keys)
WANTED_TAGS = Param("tags", "Comma-separated tags (or repeat the parameter). At most 100 without authentication.")
PROVIDER = Param("provider", "Provider slug, to disambiguate a GID shared by several providers.")
INCLUDE_DOWNLOADS = Param(
    "include_downloads",
    "Include download status and progress (download_status) for each archive. "
    "Requires authentication and viewer.download_history.",
    type="boolean",
)
DOWNLOADING_ONLY = Param(
    "downloading_only",
    "Only include archives that are currently downloading (incomplete / active download).",
    type="boolean",
)
GALLERY_STATUS_BODY: dict[str, Any] = {
    "type": "object",
    "required": ["status"],
    "properties": {
        "status": {"type": "string", "enum": ["deleted", "denied", "normal"]},
        "reason": {"type": "string", "maxLength": 200},
    },
}
DELETE_OPTIONS_BODY: dict[str, Any] = {
    "type": "object",
    "description": "Flags can also be passed as query parameters. Without archive flags, the archive entry and its "
    "file are deleted. delete_zipped recycles the archive instead.",
    "properties": {
        "delete_archive": {"type": "boolean"},
        "delete_file": {"type": "boolean"},
        "delete_zipped": {"type": "boolean"},
        "mark_gallery_deleted": {"type": "boolean", "description": "Needs viewer.mark_delete_gallery."},
        "mark_gallery_denied": {"type": "boolean", "description": "Needs viewer.approve_gallery."},
        "delete_gallery": {"type": "boolean", "description": "Needs viewer.delete_gallery."},
        "reason": {"type": "string", "maxLength": 200},
    },
}
ARCHIVE_REF = {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}
ARCHIVE_GROUP_ENTRY_BODY: dict[str, Any] = {
    "type": "object",
    "required": ["archive"],
    "properties": {"title": {"type": "string"}, "position": {"type": "integer"}, "archive": ARCHIVE_REF},
}
ARCHIVE_GROUP_BODY: dict[str, Any] = {
    "type": "object",
    "required": ["title", "details", "position", "archive_group_entries"],
    "properties": {
        "title": {"type": "string"},
        "title_slug": {"type": "string", "description": "Create only."},
        "details": {"type": "string"},
        "position": {"type": "integer"},
        "archive_group_entries": {"type": "array", "items": ARCHIVE_GROUP_ENTRY_BODY},
    },
}
WANTED_GALLERY_BODY: dict[str, Any] = {
    "type": "object",
    "description": "Fields to set; on update, missing fields are left as they are.",
    "properties": {
        **{
            name: {"type": "string"}
            for name in (
                "title",
                "title_jpn",
                "search_title",
                "unwanted_title",
                "category",
                "reason",
                "book_type",
                "publisher",
                "match_expression",
                "exclusive_scope_name",
                "wanted_tags_accept_if_none_scope",
            )
        },
        **{
            name: {"type": "boolean"}
            for name in (
                "regexp_search_title",
                "regexp_search_title_icase",
                "regexp_unwanted_title",
                "regexp_unwanted_title_icase",
                "wanted_tags_exclusive_scope",
                "should_search",
                "keep_searching",
                "notify_when_found",
                "restricted_to_links",
                "public",
                "found",
            )
        },
        **{name: {"type": "integer"} for name in ("wanted_page_count_lower", "wanted_page_count_upper", "page_count")},
        "wait_for_time": {"type": "string", "description": "Seconds or an ISO 8601 / Django duration."},
        "release_date": {"type": "string", "description": "Timestamp, date or datetime."},
        "date_found": {"type": "string", "description": "Timestamp, date or datetime."},
        "add_to_archive_group": {"type": ["integer", "null"]},
        "wanted_tags": {"type": "array", "items": {"type": "string"}},
        "unwanted_tags": {"type": "array", "items": {"type": "string"}},
        "wanted_providers": {"type": "array", "items": {"type": "string"}},
        "unwanted_providers": {"type": "array", "items": {"type": "string"}},
        "categories": {"type": "array", "items": {"type": "string"}},
    },
}
UPDATE_METADATA_BODY: dict[str, Any] = {
    "type": "object",
    "description": "Identify the gallery with gid (plus provider), gids, gallery or archive; these can also be "
    "query parameters.",
    "properties": {
        "gid": {"type": "string"},
        "gids": {"type": "array", "items": {"type": "string"}},
        "provider": {"type": "string"},
        "gallery": {"type": "integer"},
        "archive": {"type": "integer"},
        "reason": {"type": "string", "maxLength": 200},
    },
}
TAG_ARCHIVES = "Archives"
TAG_GALLERIES = "Galleries"
TAG_GROUPS = "Archive groups"
TAG_WANTED = "Wanted galleries"
TAG_READER = "Reader"
TAG_TASKS = "Tasks"
TAG_USER = "User"

GET_COMMANDS: list[Command] = [
    # Newer commands go first: their keys can't appear in older requests, and a request such as
    # ?archives-simple&q=... must not be taken by the q command.
    Command(
        "GET",
        "task",
        handle_get_task,
        "Status of a background task",
        TAG_TASKS,
        key_param=_id("task", "Task"),
        auth_required=True,
        description="Status, timestamps (Unix seconds), error message and result summary of a task queued by a "
        "write command. Only staff and the user who queued the task can see it.",
        response={"$ref": "#/components/schemas/Task"},
    ),
    Command(
        "GET",
        "tasks",
        handle_get_tasks,
        "Status of several background tasks",
        TAG_TASKS,
        key_param=Param("tasks", "Comma-separated task IDs (or repeat the parameter), at most 500.", required=True),
        auth_required=True,
        description="Tasks you can't see, or that don't exist, are reported with status missing.",
        response={"$ref": "#/components/schemas/TaskBatch"},
    ),
    Command("GET", "me", handle_get_me, "The authenticated user", TAG_USER, auth_required=True),
    Command(
        "GET",
        "archive-reader",
        handle_get_archive_reader,
        "Archive details for the image viewer",
        TAG_READER,
        key_param=_id("archive-reader", "Archive"),
        params=(Param("position", "Position of the startImage to include (default 1).", "integer"),),
        description="Anonymous callers only see public, extracted archives.",
    ),
    Command(
        "GET",
        "archive-images",
        handle_get_archive_images,
        "Extracted images of an archive",
        TAG_READER,
        key_param=_id("archive-images", "Archive"),
        params=(Param("i", "Image position (repeat the parameter); results keep this order.", "integer", True, True),),
    ),
    Command(
        "GET",
        "archives-browse",
        handle_get_archives_browse,
        "Paginated archive list, filtered like the archive search page",
        TAG_READER,
        params=(*ARCHIVE_FILTERS, Param("page", type="integer"), Param("limit", "1-100, default 48.", "integer")),
    ),
    Command(
        "GET",
        "archives-quick",
        handle_get_archives_quick,
        "Paginated quick archive search",
        TAG_READER,
        params=(
            Param("title"),
            Param("q", "Space-separated tag terms (^ exact, - exclude)."),
            Param("filename"),
            Param("rating_from", type="number"),
            Param("rating_to", type="number"),
            Param("filecount_from", type="integer"),
            Param("filecount_to", type="integer"),
            Param("filesize_from", type="integer"),
            Param("filesize_to", type="integer"),
            Param("posted_from"),
            Param("posted_to"),
            Param("match_type"),
            Param("source_type"),
            Param("extracted", "Only extracted archives."),
            Param("favorites", "Favorite group of the authenticated user.", "integer"),
            Param("order", "create_date (default), rating, posted, or another archive field."),
            Param("asc", "Ascending order."),
            Param("page", type="integer"),
            Param("limit", "1-100, default 48.", "integer"),
        ),
    ),
    Command(
        "GET",
        "archives-simple",
        handle_get_archives_simple,
        "Archive IDs and titles matching a title or tags",
        TAG_READER,
        params=(Param("q", "Title words or comma-separated tags."),),
    ),
    Command(
        "GET",
        "archive",
        handle_get_archive,
        "Archive details",
        TAG_ARCHIVES,
        key_param=_id("archive", "Archive"),
    ),
    Command(
        "GET",
        "archives",
        handle_get_archives,
        "Several archives",
        TAG_ARCHIVES,
        key_param=_id("archives", "Archive", multi=True),
    ),
    Command("GET", "at", handle_get_at, "Sorted tags of an archive", TAG_ARCHIVES, key_param=_id("at", "Archive")),
    Command("GET", "ah", handle_get_ah, "Image SHA-1 hashes of an archive", TAG_ARCHIVES, key_param=_id("ah", "Archive")),
    Command(
        "GET",
        "aof",
        handle_get_aof,
        "Non-image files inside an archive",
        TAG_ARCHIVES,
        key_param=_id("aof", "Archive"),
    ),
    Command(
        "GET",
        "aid",
        handle_get_aid,
        "Per-image metadata of archives",
        TAG_ARCHIVES,
        key_param=_id("aid", "Archive", multi=True),
    ),
    Command(
        "GET",
        "gallery",
        handle_get_gallery,
        "Gallery by internal ID",
        TAG_GALLERIES,
        key_param=_id("gallery", "Gallery"),
        params=(INCLUDE_DOWNLOADS, DOWNLOADING_ONLY),
    ),
    Command(
        "GET",
        "gids",
        handle_get_gids,
        "Galleries by provider GID",
        TAG_GALLERIES,
        key_param=Param(
            "gids",
            "Provider GID (repeat the parameter). At most 100 without authentication.",
            required=True,
            multi=True,
        ),
        params=(PROVIDER, INCLUDE_DOWNLOADS, DOWNLOADING_ONLY),
    ),
    Command(
        "GET",
        "gid",
        handle_get_gid,
        "Gallery by provider GID",
        TAG_GALLERIES,
        key_param=Param("gid", "Provider GID.", required=True),
        params=(PROVIDER, INCLUDE_DOWNLOADS, DOWNLOADING_ONLY),
    ),
    Command("GET", "gt", handle_get_gt, "Sorted tags of a gallery", TAG_GALLERIES, key_param=_id("gt", "Gallery")),
    Command(
        "GET",
        "sha1",
        handle_get_sha1,
        "Archives containing an image with this SHA-1",
        TAG_ARCHIVES,
        key_param=Param("sha1", "Image SHA-1 hash.", required=True),
    ),
    Command(
        "GET",
        "qa",
        handle_get_qa,
        "Filtered archive search (compact results)",
        TAG_ARCHIVES,
        params=ARCHIVE_FILTERS,
    ),
    Command(
        "GET",
        "q",
        handle_get_q,
        "Title and tag archive search (compact results)",
        TAG_ARCHIVES,
        key_param=Param("q", "Title words or comma-separated tags.", required=True),
    ),
    Command(
        "GET",
        "match",
        handle_get_match,
        "Filtered gallery search for matchers",
        TAG_GALLERIES,
        params=GALLERY_FILTERS,
    ),
    Command("GET", "g", handle_get_g, "Filtered gallery search (minimal results)", TAG_GALLERIES, params=GALLERY_FILTERS),
    Command(
        "GET",
        "gc",
        handle_get_gc,
        "Filtered gallery search (crawler metadata)",
        TAG_GALLERIES,
        params=GALLERY_FILTERS,
    ),
    Command(
        "GET",
        "gs",
        handle_get_gs,
        "Filtered gallery search with archives",
        TAG_GALLERIES,
        params=GALLERY_FILTERS,
    ),
    Command(
        "GET",
        "gsp",
        handle_get_gsp,
        "Paginated gallery search with archives",
        TAG_GALLERIES,
        params=(
            *GALLERY_FILTERS,
            Param("page", type="integer"),
            Param("count", "Page size: 24, 48 (default), 100, 200 or 300.", "integer"),
        ),
    ),
    Command(
        "GET",
        "gd",
        handle_get_gd,
        "Gallery details with archive downloads",
        TAG_GALLERIES,
        key_param=_id("gd", "Gallery"),
    ),
    Command(
        "GET",
        "as",
        handle_get_as,
        "Archive search, grouped by gallery",
        TAG_ARCHIVES,
        params=ARCHIVE_FILTERS,
    ),
    Command(
        "GET",
        "archive-group",
        handle_get_archive_group,
        "Archive group with its entries",
        TAG_GROUPS,
        key_param=_id("archive-group", "Archive group"),
    ),
    Command(
        "GET",
        "archive-group-entry",
        handle_get_archive_group_entry,
        "Archive group entry",
        TAG_GROUPS,
        key_param=_id("archive-group-entry", "Archive group entry"),
    ),
    Command(
        "GET",
        "archive-group-entry-archive",
        handle_get_archive_group_entry_archive,
        "Archive formatted as an archive group entry",
        TAG_GROUPS,
        key_param=_id("archive-group-entry-archive", "Archive"),
    ),
    Command(
        "GET",
        "archive-wanted-image",
        handle_get_archive_wanted_image,
        "Match an archive's images against wanted images",
        TAG_WANTED,
        key_param=_id("archive-wanted-image", "Archive"),
    ),
    Command(
        "GET",
        "wanted-gallery",
        handle_get_wanted_gallery,
        "Wanted gallery",
        TAG_WANTED,
        key_param=_id("wanted-gallery", "Wanted gallery"),
        params=(Param("include_found_galleries", "Include the found galleries."),),
    ),
    Command(
        "GET",
        "wanted-galleries",
        handle_get_wanted_galleries,
        "Several wanted galleries",
        TAG_WANTED,
        key_param=Param(
            "wanted-galleries",
            "Wanted gallery IDs (repeat the parameter). At most 100 without authentication.",
            "integer",
            True,
            True,
        ),
        params=(Param("include_found_galleries", "Include the found galleries."),),
    ),
    Command(
        "GET",
        "wanted-galleries-search",
        handle_get_wanted_galleries_search,
        "Filtered wanted gallery search",
        TAG_WANTED,
        params=WANTED_FILTERS,
        description="Unpaginated. Without authentication, only the first 200 results are returned.",
    ),
    Command(
        "GET",
        "wanted-tags",
        handle_get_wanted_tags,
        "Wanted galleries by wanted tag",
        TAG_WANTED,
        params=(WANTED_TAGS,),
    ),
    Command(
        "GET",
        "wanted-tags-search",
        handle_get_wanted_tags,
        "Wanted galleries by wanted tag (alias of wanted-tags)",
        TAG_WANTED,
        params=(WANTED_TAGS,),
    ),
]

POST_COMMANDS: list[Command] = [
    Command(
        "POST",
        "update-metadata",
        handle_post_update_metadata,
        "Queue a gallery metadata update",
        TAG_GALLERIES,
        perm="viewer.update_metadata",
        params=(Param("gid"), Param("gids", multi=True), PROVIDER, Param("gallery", type="integer"), Param("archive")),
        body=UPDATE_METADATA_BODY,
        queues_tasks=True,
    ),
    Command(
        "POST",
        "extract-archive",
        handle_post_extract_archive,
        "Queue extraction of an archive's images",
        TAG_READER,
        key_param=_id("extract-archive", "Archive"),
        perm="viewer.expand_archive",
        params=(Param("resized", type="boolean", description="Extract resized images when enabled."),),
        queues_tasks=True,
    ),
    Command(
        "POST",
        "reduce-archive",
        handle_post_reduce_archive,
        "Queue removal of an archive's extracted images",
        TAG_READER,
        key_param=_id("reduce-archive", "Archive"),
        perm="viewer.expand_archive",
        queues_tasks=True,
    ),
    Command(
        "POST",
        "archive-group",
        handle_post_archive_group,
        "Create an archive group",
        TAG_GROUPS,
        perm="viewer.change_archivegroup",
        body=ARCHIVE_GROUP_BODY,
    ),
    Command(
        "POST",
        "archive-group-entry",
        handle_post_archive_group_entry,
        "Add an entry to an archive group",
        TAG_GROUPS,
        key_param=_id("archive-group-entry", "Archive group"),
        perm="viewer.change_archivegroupentry",
        body=ARCHIVE_GROUP_ENTRY_BODY,
    ),
    Command(
        "POST",
        "wanted-gallery",
        handle_post_wanted_gallery,
        "Create a wanted gallery",
        TAG_WANTED,
        perm="viewer.add_wantedgallery",
        body=WANTED_GALLERY_BODY,
    ),
]

PUT_COMMANDS: list[Command] = [
    Command(
        "PUT",
        "archive-group",
        handle_put_archive_group,
        "Update an archive group and its entries",
        TAG_GROUPS,
        key_param=_id("archive-group", "Archive group"),
        perm="viewer.change_archivegroup",
        body=ARCHIVE_GROUP_BODY,
    ),
    Command(
        "PUT",
        "archive-group-entry",
        handle_put_archive_group_entry,
        "Update an archive group entry",
        TAG_GROUPS,
        key_param=_id("archive-group-entry", "Archive group entry"),
        perm="viewer.change_archivegroupentry",
        body=ARCHIVE_GROUP_ENTRY_BODY,
    ),
    Command(
        "PUT",
        "wanted-gallery",
        handle_put_wanted_gallery,
        "Update a wanted gallery",
        TAG_WANTED,
        key_param=_id("wanted-gallery", "Wanted gallery"),
        perm="viewer.change_wantedgallery",
        body=WANTED_GALLERY_BODY,
    ),
    Command(
        "PUT",
        "gid",
        handle_put_gid,
        "Change a gallery's status, by provider GID",
        TAG_GALLERIES,
        key_param=Param("gid", "Provider GID.", required=True),
        params=(PROVIDER,),
        perm=("viewer.mark_delete_gallery", "viewer.approve_gallery"),
        description="deleted and normal need viewer.mark_delete_gallery, denied needs viewer.approve_gallery.",
        body=GALLERY_STATUS_BODY,
    ),
    Command(
        "PUT",
        "gallery",
        handle_put_gallery,
        "Change a gallery's status, by internal ID",
        TAG_GALLERIES,
        key_param=_id("gallery", "Gallery"),
        perm=("viewer.mark_delete_gallery", "viewer.approve_gallery"),
        description="deleted and normal need viewer.mark_delete_gallery, denied needs viewer.approve_gallery.",
        body=GALLERY_STATUS_BODY,
    ),
]

DELETE_COMMANDS: list[Command] = [
    Command(
        "DELETE",
        "archive-group",
        handle_delete_archive_group,
        "Delete an archive group",
        TAG_GROUPS,
        key_param=_id("archive-group", "Archive group"),
        perm="viewer.delete_archivegroup",
    ),
    Command(
        "DELETE",
        "archive-group-entry",
        handle_delete_archive_group_entry,
        "Delete an archive group entry",
        TAG_GROUPS,
        key_param=_id("archive-group-entry", "Archive group entry"),
        perm="viewer.delete_archivegroupentry",
    ),
    Command(
        "DELETE",
        "wanted-gallery",
        handle_delete_wanted_gallery,
        "Delete a wanted gallery",
        TAG_WANTED,
        key_param=_id("wanted-gallery", "Wanted gallery"),
        perm="viewer.delete_wantedgallery",
    ),
    Command(
        "DELETE",
        "archive",
        handle_delete_archive,
        "Queue an archive's deletion",
        TAG_ARCHIVES,
        key_param=_id("archive", "Archive"),
        perm="viewer.delete_archive",
        body=DELETE_OPTIONS_BODY,
        queues_tasks=True,
    ),
    Command(
        "DELETE",
        "gid",
        handle_delete_gid,
        "Queue the deletion of a gallery's archives, by provider GID",
        TAG_GALLERIES,
        key_param=Param("gid", "Provider GID.", required=True),
        params=(PROVIDER, Param("archive_ids", "Only these archives of the gallery.", "integer", multi=True)),
        perm="viewer.delete_archive",
        body=DELETE_OPTIONS_BODY,
        queues_tasks=True,
    ),
    Command(
        "DELETE",
        "gallery",
        handle_delete_gallery,
        "Queue the deletion of a gallery's archives, by internal ID",
        TAG_GALLERIES,
        key_param=_id("gallery", "Gallery"),
        params=(Param("archive_ids", "Only these archives of the gallery.", "integer", multi=True),),
        perm="viewer.delete_archive",
        body=DELETE_OPTIONS_BODY,
        queues_tasks=True,
    ),
]

COMMANDS: dict[str, list[Command]] = {
    "GET": GET_COMMANDS,
    "POST": POST_COMMANDS,
    "PUT": PUT_COMMANDS,
    "DELETE": DELETE_COMMANDS,
}
