# Panda Backup Public JSON API

This documentation describes the public-facing, third-party JSON API for Panda Backup.

**Interactive docs:** `/api/v1/docs/` (Swagger UI, with "Try it out") and the OpenAPI 3.1 document at
`/api/v1/openapi.json`, both generated from the command registry in
[`viewer/views/api/registry.py`](../../viewer/views/api/registry.py). The Markdown files in this folder have the
detailed request and response examples.

**Static copy:** [`openapi.json`](openapi.json) in this folder is the same document, with the public instance as its
server; GitLab shows it with Swagger UI when you open the file. Regenerate it after changing the registry with
`python manage.py openapi_schema --update` (a test fails while it is out of date). `openapi_schema --output <file>
--server <url>` exports it for another instance, for example to build static docs with Redocly or Swagger UI.

## Routes & Versions

| Route | Status | Notes |
| :--- | :--- | :--- |
| `/api/v1/` | **Current** | Versioned command API. Errors use only the [error envelope](#errors). A bad or expired bearer token is an error (`401`). |
| `/api/v1/remote/` | **Current** | Remote operations API (userscript, other instances), see [json_parser.md](json_parser.md). |
| `/api/v1/openapi.json`, `/api/v1/docs/` | **Current** | OpenAPI document and interactive docs. |
| `/api/`, `/api`, `/jsearch/`, `/jsearch` | Supported | Same commands as `/api/v1/`, kept for existing public clients. Errors also repeat the message in `result`. A bad token is treated as no token. |
| `/r-api/...` | Deprecated | The old reader API (from `viewer/views/exp.py`), now served by the reader commands. Responses carry `Deprecation: true` and a `Link: <...>; rel="successor-version"` header. |
| `/admin-api/`, `/jsonapi/` | Deprecated | Aliases of `/api/v1/remote/`, with the same deprecation headers. |

`/r-api/` successors:

| Legacy route | `/api/v1/` command |
| :--- | :--- |
| `GET /r-api/archive/<id>/[?position=N]` | `GET /api/v1/?archive-reader=<id>[&position=N]` |
| `GET /r-api/archive/<id>/image_list/?i=1&i=2` | `GET /api/v1/?archive-images=<id>&i=1&i=2` |
| `POST /r-api/archive/<id>/extract_archive/` | `POST /api/v1/?extract-archive=<id>` |
| `POST`/`DELETE /r-api/archive/<id>/reduce_archive/` | `POST /api/v1/?reduce-archive=<id>` |
| `GET /r-api/archive/` | `GET /api/v1/?archives-quick` |
| `GET /r-api/archives/` | `GET /api/v1/?archives-browse` |
| `GET /r-api/archives_simple/?q=` | `GET /api/v1/?archives-simple&q=` |
| `GET /r-api/me/` | `GET /api/v1/?me` (`401` for anonymous callers; `/r-api/me/` keeps returning empty fields) |

> [!NOTE]
> **Trailing Slashes**: canonical routes use trailing slashes (`/api/v1/`, `/api/`, `/jsearch/`). `/api` and
> `/jsearch` without the trailing slash are supported for backwards compatibility.

---

## Architecture & Request Dispatch

Rather than relying on separate URL paths for every resource, the API uses a **query-parameter dispatch architecture** over standard HTTP methods (`GET`, `POST`, `PUT`, `DELETE`).

Each request is directed to `/api/v1/` (or a legacy alias), and the **first matching command parameter** in the query string, in registry order, dictates which handler processes the request (defined in [`viewer/views/api/registry.py`](../../viewer/views/api/registry.py) and [`viewer/views/api/dispatch.py`](../../viewer/views/api/dispatch.py)).

For example:
- `GET /api/v1/?gid=12345` &rarr; dispatches to the single gallery GID handler.
- `GET /api/v1/?archive=42` &rarr; dispatches to the single archive handler.
- `POST /api/v1/?wanted-gallery=` &rarr; dispatches to the create wanted gallery handler.
- `DELETE /api/v1/?archive-group=5` &rarr; dispatches to the delete archive group handler.
- `GET /api/v1/?task=17` &rarr; status of background task 17.

`GET /api/v1/` without any parameter returns links to the OpenAPI document and the interactive docs.

If no recognized command parameter is present, the API returns `400 Bad Request` with the `unknown_command` error
code. Other HTTP methods (such as `PATCH` or `HEAD`) return `405 Method Not Allowed` with an `Allow` header.

Request bodies (`POST`, `PUT`, `DELETE`) are JSON objects (`Content-Type: application/json`). An invalid or
non-object body returns `400` (`invalid_json`); a non-empty form-encoded body returns `415`.

---

## Errors

Every API error has the same envelope and a matching HTTP status:

```json
{
  "error": {
    "code": "not_found",
    "message": "Archive does not exist."
  }
}
```

| Status | Codes | When |
| :--- | :--- | :--- |
| `400` | `bad_request`, `unknown_command`, `invalid_json`, `missing_field`, `empty_body` | Malformed request, unknown command, invalid parameter or body. |
| `401` | `not_authenticated`, `invalid_token` | The command needs authentication, or (on `/api/v1/`) the bearer token is unknown, expired or revoked. Has a `WWW-Authenticate: Bearer` header. |
| `403` | `forbidden`, `insufficient_scope` | The user lacks the Django permission, or (on `/api/v1/`) the token lacks the scope. |
| `404` | `not_found` | The object does not exist or isn't visible to the caller. |
| `405` | `method_not_allowed` | Unsupported HTTP method. Has an `Allow` header. |
| `415` | `unsupported_media_type` | Form-encoded body instead of JSON. |
| `500` | `internal_error` | Unexpected server error (logged). |

`code` values are stable; `message` is for humans and may change.

Legacy routes add the keys their clients used to read: `/api/` and `/jsearch/` repeat the message in `result`
(`{"error": {...}, "result": "Archive does not exist."}`), `/r-api/` adds `"result": "error"` and `message`, and
`/admin-api/` adds `message`.

---

## Background Tasks

Write commands that queue worker tasks (`DELETE ?archive=`, `DELETE ?gid=`, `DELETE ?gallery=`,
`POST ?update-metadata=`, and the queueing operations of `/api/v1/remote/`) answer with a task reference:

- one task: `"task_id": 17, "status_url": "/api/v1/?task=17"`
- several tasks: `"task_ids": [17, 18], "status_url": "/api/v1/?tasks=17,18"` (commands that answer with
  `task_ids` always point to `?tasks=`, even when they queued a single task, so the polled shape doesn't change)
- nothing queued: `task_id`/`status_url` are `null`.

`status_url` points to the API surface the request was made on (`/api/?task=` for requests to `/api/`).

### Task Status (`GET ?task=<id>`)

Needs authentication (token `read` scope or a session). Only staff and the user who queued the task can see it;
other tasks return `404`.

```json
{
  "id": 17,
  "task_type": "delete_archive_worker",
  "status": "completed",
  "active": false,
  "label": "Deleting",
  "done_label": "Deleted",
  "reason": "",
  "created_at": 1795219200,
  "started_at": 1795219201,
  "finished_at": 1795219203,
  "not_before": null,
  "attempts": 1,
  "max_attempts": 3,
  "error_message": "",
  "result": {"outcome": "completed", "targets": [42], "delete_archive": true, "delete_file": true},
  "poll_delay_ms": 0,
  "worker_running": true,
  "status_url": "/api/v1/?task=17"
}
```

- `status`: `pending`, `processing`, `completed`, `failed` or `cancelled`. Poll while `active` is true, waiting
  `poll_delay_ms` between requests.
- Timestamps are Unix seconds (`null` when not reached yet).
- `result` is `null` while the task is active. Then it has the `outcome`, the task's `targets` (its arguments, such
  as the archive ID or the links), the deletion options for delete tasks and `crawl_stats` for crawler tasks.
  `details` holds what the worker recorded when the task completed, for the task types that record something:
  the `requested`/`fetched`/`updated`/`failed`/`unsupported` counts for `gallery_updater` (`?update-metadata=`), the
  pruned row counts for `database_prune`, the counters of the check for `archive_file_check`.
- `error_message` is set for failed tasks.

### Batch Task Status (`GET ?tasks=<id1>,<id2>`)

Comma-separated IDs, or the parameter repeated, at most 500. Tasks that don't exist or that you can't see are
returned with `"status": "missing"`. The top-level keys are the same as those of `/api/tasks/status/?ids=` (the
endpoint Archive Manager polls), so a client can switch to the `status_url` of a command response without changes.

```json
{
  "total": 2,
  "pending": 0,
  "processing": 1,
  "completed": 0,
  "failed": 0,
  "cancelled": 0,
  "missing": 1,
  "active": true,
  "done": 1,
  "worker_running": true,
  "poll_delay_ms": 1500,
  "status_url": "/api/v1/?tasks=17,18",
  "tasks": [
    {"id": 17, "status": "processing", "...": "..."},
    {"id": 18, "task_type": "", "status": "missing", "active": false, "label": "Task", "done_label": "Done", "error_message": ""}
  ]
}
```

---

## Authentication & Authorization

Authentication is optional for read requests, but mandatory for non-public records and write operations:

### 1. Unauthenticated Requests
- Can read any entity marked as **public** (`public=true`).
- Requests for non-public entities return `404 Not Found` (`not_found`).
- Commands that need a user (write commands, `task`, `tasks`, `me`) return `401 Unauthorized` (`not_authenticated`).
- In search/list endpoints, non-public items are excluded from the result set.

### 2. Authenticated Requests
Authenticated callers have access to both public and private entities, internal creation dates, and administrative/write features.

Authentication is accepted via:
- **API Token (Long-Lived Bearer Token)**: Passed via the `Authorization` HTTP header:
  ```http
  Authorization: Bearer YOUR_API_TOKEN
  ```
  Tokens are verified against user tokens in the database with expiry checking. On `/api/v1/`, a token that is
  expired, revoked or belongs to a disabled user returns `401` (`invalid_token`), and a token without the scope the
  request needs returns `403` (`insufficient_scope`). On the legacy routes such a token is treated as no token.

  Create, list and revoke your tokens from the profile page (**Long-lived Access Tokens**), or with
  `manage.py user-management -un <user> -ct <name> [--token-expiry-days N] [--token-scopes read,write]`.
  Each token has:

  | Property | Description |
  |----------|-------------|
  | Expiry | A chosen number of days, or **no expiry** (`--token-expiry-days 0`). Tokens created before expiry became optional keep their original date. |
  | Scopes | `read`: GET requests, images, downloads, task status. `write`: POST/PUT/DELETE on `/api/v1/` (and `/api/`), `/r-api/` state-changing actions and archive management. `remote`: the `/api/v1/remote/` operations (also requires the `viewer.use_remote_api` permission). Existing tokens have all scopes. |
  | Last used | Updated when the token authenticates, at most once a minute. |

  Scopes only restrict a token; they never give it more than its user is allowed to do. Session logins are not limited by scopes.
- **Session Cookie**: Standard Django session authentication (e.g. active web session or via API login).

### 3. Login, Logout, and State-Changing Web Action Endpoints
For session-based integration, helper endpoints are available in [`viewer/views/api/routes.py`](../../viewer/views/api/routes.py):

- **Login**: `POST /api-login/`  
  Form-data parameters: `username`, `password`.  
  Response on success:
  ```json
  {"success": true}
  ```
  Response on failure:
  ```json
  {"success": false, "message": "Invalid login credentials."}
  ```
- **Logout**: `POST /api-logout/`  
  Clears the session. Response:
  ```json
  {"success": true, "message": "Logged out."}
  ```
- **Web Action Routes (`POST`-only)**: All state-changing web/collaborator/admin routes require HTTP `POST` (with Django CSRF protection for session-authenticated browsers) and return `405 Method Not Allowed` on `GET`. This includes `/logout/`, `/web-queue/<operation>/<arguments>/`, `/user-archive-preferences/<archive_pk>/<setting>/`, `/archive/<id>/extract/`, `/archive/<id>/reduce/`, `/archive/<id>/check-convert-type/`, `/archive/<id>/mark-similar-archives/`, `/archive/<id>/generate-matches/`, `/update/<id>/...`, `/col-update/<id>/...`, `/col-gallery-update/<id>/...`, `/gallery/<id>/<tool>/`, `/gallery-find/<tool>/`, `/wanted-gallery/<id>/` tool actions, and `/archive-group/<id|slug>/` (`extract_all`). Automation and third-party scripts should use the token-authenticated `/api/v1/` and `/api/v1/remote/` endpoints instead.

### 4. Permissions for Write Operations
All `POST`, `PUT`, and `DELETE` requests require an authenticated user with specific model permissions:

| Operation | Command Parameter | Required Django Permission |
| :--- | :--- | :--- |
| Queue Gallery Metadata Update | `POST ?update-metadata=` | `viewer.update_metadata` |
| Extract / Reduce Archive | `POST ?extract-archive=<id>` / `POST ?reduce-archive=<id>` | `viewer.expand_archive` |
| Create Archive Group | `POST ?archive-group=` | `viewer.change_archivegroup` |
| Add Archive Group Entry | `POST ?archive-group-entry=<group_id>` | `viewer.change_archivegroupentry` |
| Create Wanted Gallery | `POST ?wanted-gallery=` | `viewer.add_wantedgallery` |
| Update Gallery Status (by GID or ID) | `PUT ?gid=<gid>` / `PUT ?gallery=<id>` | `viewer.mark_delete_gallery` (`deleted`/`normal`) or `viewer.approve_gallery` (`denied`) |
| Update Archive Group | `PUT ?archive-group=<group_id>` | `viewer.change_archivegroup` |
| Update Archive Group Entry | `PUT ?archive-group-entry=<entry_id>` | `viewer.change_archivegroupentry` |
| Update Wanted Gallery | `PUT ?wanted-gallery=<id>` | `viewer.change_wantedgallery` |
| Delete Archive (by Archive ID or Gallery GID/ID) | `DELETE ?archive=<id>` / `DELETE ?gid=<gid>` / `DELETE ?gallery=<id>` | `viewer.delete_archive` (plus gallery perms if gallery action flags are passed) |
| Delete Archive Group | `DELETE ?archive-group=<group_id>` | `viewer.delete_archivegroup` |
| Delete Archive Group Entry | `DELETE ?archive-group-entry=<entry_id>` | `viewer.delete_archivegroupentry` |
| Delete Wanted Gallery | `DELETE ?wanted-gallery=<id>` | `viewer.delete_wantedgallery` |

Requests without authentication return `401 Unauthorized`; authenticated requests without the permission return
`403 Forbidden`:
```json
{
  "error": {"code": "forbidden", "message": "Not authorized"}
}
```

---

## Common Tag Query & Filter Syntax

Many search endpoints (`g`, `gc`, `gs`, `gsp`, `qa`, `q`, `as`, `wanted-galleries-search`) accept tag queries via `tags=`. The tag syntax supports prefixes and namespaces:

- **Namespaces**: Format as `scope:tag_name` (e.g. `artist:alpha`, `female:schoolgirl`, `language:english`).
- **Whitespace**: Spaces are automatically replaced with underscores (`_`).
- **Multiple Tags**: Comma-separated list (`tags=artist:alpha,female:schoolgirl`).
- **Prefix Modifiers**:
  - Default (no prefix): Substring / contains match (`tags=schoolgirl`).
  - `^` : Exact tag match (`tags=^female:schoolgirl`).
  - `-` : Negate / exclude substring match (`tags=-male:ugly_bastard`).
  - `-^` : Negate exact tag match (`tags=-^female:nekomimi`).

---

## API Documentation Directory

Detailed endpoint references and request/response examples are organized into the following documents:

| Documentation File | Topic | Available Command Parameters |
| :--- | :--- | :--- |
| **[galleries.md](galleries.md)** | Galleries & Provider Metadata | `gid`, `gids`, `gallery`, `gd`, `gt`, `g`, `gc`, `gs`, `gsp`, `match` |
| **[archives.md](archives.md)** | Archives, Images & Downloads | `archive`, `archives`, `at`, `ah`, `aof`, `aid`, `sha1`, `qa`, `q`, `as`, `archive-wanted-image` |
| **[wanted_galleries.md](wanted_galleries.md)** | Wanted Galleries & Tracking | `wanted-gallery`, `wanted-galleries`, `wanted-galleries-search`, `wanted-tags` |
| **[archive_groups.md](archive_groups.md)** | Archive Groups & Collections | `archive-group`, `archive-group-entry`, `archive-group-entry-archive` |
| **[admin_report.md](admin_report.md)** | Staff Admin Report JSON | `/admin-report/?format=json` |
| **[json_parser.md](json_parser.md)** | Remote Operations API (`/api/v1/remote/`) | `webcrawler`, `match_archive`, `match_archive_internally`, `?gc=` |

---

## Endpoint Quick Reference

Below is the complete list of available commands on `/api/v1/` (and the legacy `/api/`, `/jsearch/`):

### GET Commands
| Key | Target | Parameters | Description |
| :--- | :--- | :--- | :--- |
| `task` | Task | `?task=<id>` | Background task status *(auth required)* |
| `tasks` | Task | `?tasks=<id>,<id>...` | Batch background task status *(auth required)* |
| `me` | User | `?me` | The authenticated user *(auth required)* |
| `archive-reader` | Archive | `?archive-reader=<id>[&position=N]` | Archive details for the image viewer, with one image |
| `archive-images` | Archive | `?archive-images=<id>&i=<pos>&i=<pos>...` | Extracted image URLs and sizes, in the requested order |
| `archives-browse` | Archive | `?archives-browse[&...filters&page=&limit=]` | Paginated archive list, filtered like the search page |
| `archives-quick` | Archive | `?archives-quick[&title=&q=&order=&page=&limit=...]` | Paginated quick archive search |
| `archives-simple` | Archive | `?archives-simple&q=<query>` | Archive IDs and titles matching a title or tags |
| `archive` | Archive | `?archive=<id>` | Single archive details and relative download path |
| `archives` | Archive | `?archives=<id>&archives=<id>...` | Multiple archives by ID |
| `at` | Archive | `?at=<id>` | Sorted tag list for an archive |
| `ah` | Archive | `?ah=<id>` | Array of image SHA-1 hashes for an archive |
| `aof` | Archive | `?aof=<id>` | List of non-image extra files inside archive |
| `aid` | Archive | `?aid=<id>&aid=<id>...` | Per-page image metadata (dimensions, size, hash, format) |
| `gallery` | Gallery | `?gallery=<id>` | Single gallery by internal ID |
| `gids` | Gallery | `?gids=<gid>&gids=<gid>[&provider=<p>]` | Multiple galleries by provider GID |
| `gid` | Gallery | `?gid=<gid>[&provider=<p>]` | Single gallery by provider GID |
| `gt` | Gallery | `?gt=<id>` | Sorted tag list for a gallery |
| `sha1` | Archive | `?sha1=<hash>` | Lookup archives containing an image with this SHA-1 |
| `qa` | Archive | `?qa=[&...filters]` | Filtered archive search returning compact objects |
| `q` | Archive | `?q=<query>` | Simple text & tag search returning compact objects |
| `match` | Gallery | `?match=[&...filters]` | Filtered gallery search for matching tools |
| `g` | Gallery | `?g=[&...filters]` | Filtered gallery search returning minimal objects |
| `gc` | Gallery | `?gc=[&...filters]` | Gallery search returning crawler metadata |
| `gs` | Gallery | `?gs=[&...filters]` | Gallery search returning full objects with archives |
| `gsp` | Gallery | `?gsp=[&page=1&count=48&...filters]` | Paginated gallery search |
| `gd` | Gallery | `?gd=<id>` | Detailed gallery object with full archive download info |
| `as` | Archive/Gallery | `?as=[&...filters]` | Archive search returning grouped gallery objects |
| `archive-group` | ArchiveGroup | `?archive-group=<id>` | Single archive group with ordered entries |
| `archive-group-entry` | ArchiveGroupEntry | `?archive-group-entry=<id>` | Single archive group entry |
| `archive-group-entry-archive` | ArchiveGroupEntry | `?archive-group-entry-archive=<id>` | Archive formatted as a group entry |
| `archive-wanted-image` | Image Match | `?archive-wanted-image=<id>` | Image similarity matching against wanted images *(auth required)* |
| `wanted-gallery` | WantedGallery | `?wanted-gallery=<id>[&include_found_galleries]` | Single wanted gallery |
| `wanted-galleries` | WantedGallery | `?wanted-galleries=<id>&wanted-galleries=<id>...` | Multiple wanted galleries |
| `wanted-galleries-search` | WantedGallery | `?wanted-galleries-search=[&...filters]` | Filtered search of wanted galleries |
| `wanted-tags` | WantedGallery | `?wanted-tags=[&tags=<tag1>,<tag2>...]` | Batch lookup wanted galleries by wanted tags |

### POST Commands
| Key | Target | Parameters | Permission | Description |
| :--- | :--- | :--- | :--- | :--- |
| `update-metadata` | Gallery | `?update-metadata=[&gid=<gid>&provider=<p>]` | `viewer.update_metadata` | Enqueue metadata update for gallery(ies) by `gid`, `gids`, `gallery`, or `archive`; returns `task_id`/`status_url` |
| `extract-archive` | Archive | `?extract-archive=<id>` | `viewer.expand_archive` | Extract an archive's images |
| `reduce-archive` | Archive | `?reduce-archive=<id>` | `viewer.expand_archive` | Remove an archive's extracted images |
| `archive-group` | ArchiveGroup | `?archive-group=` | `viewer.change_archivegroup` | Create archive group with initial entries |
| `archive-group-entry` | ArchiveGroupEntry | `?archive-group-entry=<group_id>` | `viewer.change_archivegroupentry` | Append an entry to an archive group |
| `wanted-gallery` | WantedGallery | `?wanted-gallery=` | `viewer.add_wantedgallery` | Create a new wanted gallery |

### PUT Commands
| Key | Target | Parameters | Permission | Description |
| :--- | :--- | :--- | :--- | :--- |
| `gid` | Gallery | `?gid=<gid>[&provider=<p>]` | `viewer.mark_delete_gallery` / `viewer.approve_gallery` | Change gallery status to `deleted`, `denied`, or `normal` |
| `gallery` | Gallery | `?gallery=<id>` | `viewer.mark_delete_gallery` / `viewer.approve_gallery` | Change gallery status to `deleted`, `denied`, or `normal` by internal ID |
| `archive-group` | ArchiveGroup | `?archive-group=<group_id>` | `viewer.change_archivegroup` | Update archive group metadata and entries |
| `archive-group-entry` | ArchiveGroupEntry | `?archive-group-entry=<entry_id>` | `viewer.change_archivegroupentry` | Update an existing archive group entry |
| `wanted-gallery` | WantedGallery | `?wanted-gallery=<id>` | `viewer.change_wantedgallery` | Update an existing wanted gallery |

### DELETE Commands
| Key | Target | Parameters | Permission | Description |
| :--- | :--- | :--- | :--- | :--- |
| `archive` | Archive | `?archive=<id>` | `viewer.delete_archive` | Queue deletion of an archive (and optionally its file or gallery action); returns `task_id`/`status_url` |
| `gid` | Gallery/Archive | `?gid=<gid>[&provider=<p>]` | `viewer.delete_archive` | Queue deletion of archive(s) associated with a gallery by GID; returns `task_ids`/`status_url` |
| `gallery` | Gallery/Archive | `?gallery=<id>` | `viewer.delete_archive` | Queue deletion of archive(s) associated with a gallery by internal ID; returns `task_ids`/`status_url` |
| `archive-group` | ArchiveGroup | `?archive-group=<group_id>` | `viewer.delete_archivegroup` | Delete an archive group |
| `archive-group-entry` | ArchiveGroupEntry | `?archive-group-entry=<entry_id>` | `viewer.delete_archivegroupentry` | Delete an archive group entry |
| `wanted-gallery` | WantedGallery | `?wanted-gallery=<id>` | `viewer.delete_wantedgallery` | Delete a wanted gallery |
