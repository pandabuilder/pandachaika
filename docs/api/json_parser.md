# Remote Operations API (`/api/v1/remote/`)

This document describes the remote operations JSON API, implemented in
[`viewer/views/api/handlers/remote.py`](../../viewer/views/api/handlers/remote.py) (historically the `json_parser`
view). It is used by userscripts (`panda.user.js`) and the admin UI's match/crawl buttons.

---

## Routes & Endpoints

| Route | Route Name | Notes |
| :--- | :--- | :--- |
| `/api/v1/remote/` | `viewer:api-v1-remote` | **Current.** Errors use the [error envelope](README.md#errors); a bad or expired token is a `401` error. |
| `/admin-api/` | `viewer:json-parser` | Deprecated alias. Responses carry `Deprecation: true` and `Link: </api/v1/remote/>; rel="successor-version"`. Errors also repeat the message in `message`. |
| `/jsonapi/`, `/jsonapi` | `viewer:jsonapi`, `viewer:jsonapi-slashless` | Deprecated aliases, same as `/admin-api/`. |

---

## Authentication & Authorization

Unlike the command API (which allows unauthenticated access to public galleries and archives), the remote
operations API **requires authentication and a permission** for all requests:

1. **Authentication Methods**:
   - **Session Cookie**: Standard Django session (e.g. logged-in user in the web UI).
   - **API Bearer Token**: Sent in the HTTP `Authorization` header:
     ```http
     Authorization: Bearer YOUR_API_TOKEN
     ```
     Validated against active, non-expired tokens in `UserLongLivedToken` that have the `remote` scope.

2. **Required Permission**:
   - The authenticated user must hold the `viewer.use_remote_api` Django permission.

3. **Unauthorized Responses**:
   - No session and no valid token: `401 Unauthorized`, code `not_authenticated` (on `/api/v1/remote/`, `invalid_token`
     for a bad token and `403` `insufficient_scope` for a token without the `remote` scope).
   - Authenticated without `viewer.use_remote_api`: `403 Forbidden`, code `forbidden`.
     ```json
     {"error": {"code": "forbidden", "message": "Not authorized"}}
     ```

---

## Request Architecture & Conventions

### POST Requests (JSON RPC Style)

The majority of operations in `json_parser` are invoked via HTTP `POST` with a JSON payload containing:
- `operation` (`string`): The name of the internal operation to execute.
- `args` (`dict` or `list`): Arguments specific to the operation.
- Optional top-level control flags (such as `ignore_global_filters`, `archive_reason`, `archive_details`).

Headers:
```http
Content-Type: application/json
```

#### Standard Error Responses (POST)
All errors use the [error envelope](README.md#errors):

| Case | Status | Code |
| :--- | :--- | :--- |
| Empty request body | `400` | `empty_body` |
| Body is not JSON | `400` | `invalid_json` |
| Missing `operation` or `args`, or `args` of the wrong type | `400` | `bad_request` |
| Unrecognized operation | `400` | `unknown_command` |
| Archive in `match_archive`/`match_archive_internally` does not exist | `404` | `not_found` |

### GET Requests (Query Parameter Style)

HTTP `GET` requests are used exclusively for crawler-oriented gallery search via the `gc` query parameter. Without
`gc`, the API returns `400` with the `unknown_command` code.

### Unsupported HTTP Methods

Methods other than `GET` and `POST` return `405 Method Not Allowed` (`method_not_allowed`) with an `Allow` header.

### Queued Tasks

`webcrawler` adds `task_id` and `status_url` to its response (both `null` when nothing was queued). Poll `status_url` with a
token that has the `read` scope, see [Background Tasks](README.md#background-tasks).

---

## Operations Overview

| Operation / Command | Method | Target / Consumer | Description |
| :--- | :--- | :--- | :--- |
| `webcrawler` | `POST` | Internal UI, Userscripts | Queue URLs to download, check existing archives, handle parent replacement |
| `match_archive` | `POST` | Archive detail UI | Trigger background web matching for an archive |
| `match_archive_internally` | `POST` | Archive detail UI | Trigger local title similarity matching for an archive |
| `?gc=` | `GET` | Sync tools / Crawlers | Filter galleries with crawler metadata |

---

## 1. Web Crawler Enqueueing (`operation: "webcrawler"`)

Queues one or more gallery URLs as a background worker task (`task_type="web_crawler"` in `TaskQueue`). This operation supports parent gallery deletion/replacement, downloader filtering, reason tracking, and global filter overrides.

- **Method**: `POST`
- **Operation**: `"webcrawler"`

### Arguments Schema (`args`)

`args` can be passed as:
1. A **list of URLs**: `["https://example.com/g/1/...", ...]` (shorthand for `{"links": [...]}`).
2. A **dictionary** with the following fields:

| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `links` | `list[str]` or `str` | Conditional | List of URLs, or a string containing URLs separated by newlines or commas. |
| `link` | `str` or `list[str]` | Conditional | Alternative/supplemental URL or list of URLs. *(At least one of `links` or `link` must be provided).* |
| `downloader` | `str` | No | Restricts the crawler to a specific downloader module (e.g. `"fake"`, `"default"`). |
| `reason` | `str` | No | Archive reason label (stored in `archive_reason` and passed as `-reason <reason>` to worker). |
| `ignore_global_filters` / `ignore-global-filters` | `bool` | No | Bypasses crawler global filter rules and appends `--ignore-global-filters`. Can also be placed at the root level of the JSON body. |
| `parentLink` | `str` | No | URL of an existing parent gallery. Triggers confirmation or replacement workflow. |
| `action` | `str` | No | Action to take when `parentLink` is matched: `"confirmDeletion"`, `"replaceFound"`, or `"queueFound"`. |

---

### Sub-Workflows & Behaviors

#### A. Standard Crawl (New URLs)
When no `parentLink` is provided, the API checks existing database archives:
- If links are already archived, the response notes that they will be checked for redownload.
- Returns the count of submitted links in `result`.

**Example Request:**
```bash
curl -X POST "https://example.chaika.moe/api/v1/remote/" \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "operation": "webcrawler",
    "args": {
      "links": [
        "https://e-hentai.org/g/100001/token1/",
        "https://e-hentai.org/g/100002/token2/"
      ],
      "reason": "Requested via userscript"
    }
  }'
```

**Response (New Links):**
```json
{
  "message": "Crawling: 2 links",
  "result": "2",
  "task_id": 57,
  "status_url": "/api/v1/?task=57"
}
```

**Response (Some or All Archives Already Exist):**
```json
{
  "message": "Crawling 2 links (1 archives exist, checking for redownload)",
  "result": "2"
}
```
*(Or `"Archive exists, crawling to check for redownload: 2 links"` if all exist).*

---

#### B. Parent Replacement Workflow (`parentLink`)
Used when downloading an upgraded or re-uploaded version of an existing gallery.

1. **Confirmation Phase**: When `parentLink` matches an existing gallery and `action` is omitted:
   ```json
   {
     "message": "Please confirm deletion of parent: https://e-hentai.org/g/888881/ptok/",
     "action": "confirmDeletion"
   }
   ```
   *(No crawler task is queued until confirmation).*

2. **Replace Parent (`action: "replaceFound"`)**:
   - Preserves user favorite groups (`UserArchivePrefs`) and extracted state (`archive.extracted`).
   - Marks the parent gallery as deleted (`gallery.mark_as_deleted()`).
   - Deletes parent archive files and record.
   - Enqueues the new links with preserved favorite/extracted options.
   ```json
   {
     "message": "Crawling: 1 links, deleting parent: https://e-hentai.org/g/888881/ptok/",
     "result": "1"
   }
   ```

3. **Keep Parent (`action: "queueFound"`)**:
   - Leaves the parent gallery and archive intact.
   - Enqueues the new links.
   ```json
   {
     "message": "Crawling: 1 links, keeping parent: https://e-hentai.org/g/888881/ptok/",
     "result": "1"
   }
   ```

---

#### C. Downloader Restriction (`downloader`)
When `downloader` is specified in `args`, only the requested downloader is initialized:
```json
{
  "operation": "webcrawler",
  "args": {
    "link": "https://e-hentai.org/g/100001/token1/",
    "downloader": "fake"
  }
}
```

---

#### D. Error: No Links Provided
`400 Bad Request`:
```json
{
  "error": {"code": "bad_request", "message": "No links provided"}
}
```

---

## 2. Web Archive Matcher (`operation: "match_archive"`)

Matches a local archive against external web provider galleries (online mode) using `generate_possible_matches_for_archives`. Requests go through the shared database rate limiter, and providers in a long ban/cooldown are skipped (and logged).

- **Method**: `POST`
- **Operation**: `"match_archive"`
- **`args`**:

| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `archive` | `integer` | Yes | Primary key (`id`) of the archive. |
| `match_filter` | `str` | Yes | Matcher filter string (for example `chaika` or `chaika_hash`). Empty uses the enabled matchers. |
| `auto_link` | `bool` | No | Link the archive to the best match when it's good enough, instead of only storing possible matches. |

### Example Request
```bash
curl -X POST "https://example.chaika.moe/api/v1/remote/" \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "operation": "match_archive",
    "args": {
      "archive": 42,
      "match_filter": "title"
    }
  }'
```

### Response
```json
{
  "message": "web matcher done, check the logs for results"
}
```

---

## 3. Internal Archive Matcher (`operation: "match_archive_internally"`)

Matches an archive against the `Gallery` records already in the local database (offline mode): file name similarity, file size, CRC32 of other matched archives, gid, and stored image hashes.

- **Method**: `POST`
- **Operation**: `"match_archive_internally"`
- **`args`**:

| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `archive` | `integer` | Yes | Primary key (`id`) of the archive. |
| `clear` | `any` | No | If present, also removes groups, artists and other bracketed text from the file name before comparing. |
| `provider` | `str` | No | Restricts matches to a specific provider. |
| `auto_link` | `bool` | No | Link the archive to the best match when it's good enough, instead of only storing possible matches. |

- **Optional Query String Parameters**:
  - `cutoff` (`float`, default `0.4`): String similarity threshold.
  - `max-matches` (`int`, default `10`): Maximum candidate matches to record.

### Example Request
```bash
curl -X POST "https://example.chaika.moe/jsonapi/?cutoff=0.6&max-matches=5" \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "operation": "match_archive_internally",
    "args": {
      "archive": 42,
      "provider": "panda",
      "clear": true
    }
  }'
```

### Response
```json
{
  "message": "internal matcher done, check the archive for results"
}
```

---

## 4. Gallery Crawler Search (`GET ?gc=`)

Returns filtered gallery objects formatted specifically for crawler and remote sync consumption.

- **Endpoint**: `/jsonapi/` (or `/admin-api/`)
- **Method**: `GET`
- **Required Query Parameter**: `gc` (e.g. `?gc=`)

### Query Parameters
Accepts standard gallery filter keys (from `viewer.views.head.gallery_filter_keys`):
- `tags`: Tag query string (supports namespaces and `-` negation).
- `sort`: Field to sort by.
- `asc_desc`: `"asc"` or `"desc"`.
- `category`: Category filter.
- `uploader`: Uploader filter.
- *(Note: Access is authorized by the API token/session; private galleries are included by default).*

### Example Request
```bash
curl -X GET "https://example.chaika.moe/jsonapi/?gc=&tags=artist:sample&sort=posted&asc_desc=desc" \
  -H "Authorization: Bearer YOUR_API_TOKEN"
```

### Response
Returns a JSON array of gallery objects:
```json
[
  {
    "category": "Non-H",
    "comment": "Uploader note",
    "disowned": false,
    "dl_type": "info",
    "expunged": false,
    "filecount": 28,
    "filesize": 15420100,
    "fjord": false,
    "gid": "1234567",
    "hidden": false,
    "link": "https://exhentai.org/g/1234567/abcdef/",
    "posted": 1546300800,
    "provider": "panda",
    "public": true,
    "rating": "4.50",
    "tags": [
      "artist:sample",
      "female:schoolgirl",
      "language:english"
    ],
    "thumbnail": "https://example.chaika.moe/gallery/9636/thumb/",
    "thumbnail_url": "https://exhentai.org/t/1234567.jpg",
    "title": "Example Title",
    "title_jpn": "Example Japanese Title",
    "token": "abcdef1234",
    "uploader": "UploaderName"
  }
]
```
If no galleries match, returns an empty array `[]`.

---

## Comparison with the Command API

| Feature | Remote operations (`/api/v1/remote/`) | Command API (`/api/v1/`) |
| :--- | :--- | :--- |
| **Intended Audience** | Userscripts, admin UI | Public consumers, third-party apps, external clients |
| **Dispatch Style** | JSON body `{"operation": "...", "args": ...}` on `POST`; query param `?gc=` on `GET` | Query parameter dispatch on `GET`, `POST`, `PUT`, `DELETE` (`?gid=`, `?archive=`, etc.) |
| **Authentication** | Required (`remote` token scope and `viewer.use_remote_api` permission) | Optional for read (filters `public=true`), required for writes & private data |
| **Error Format** | Error envelope | Error envelope |
| **Supported Methods** | `POST`, `GET` | `GET`, `POST`, `PUT`, `DELETE` |
