# Internal Operations API Documentation (`json_parser`)

This document describes the internal operations JSON API served by the `json_parser` view in [`viewer/views/api/routes.py`](file:///home/netto/.t3/worktrees/pandagallery/t3-a5ffffb0/viewer/views/api/routes.py).

> [!WARNING]
> **Internal & Legacy API (WIP / To Be Migrated)**:
> This API is primarily intended for internal operations, management commands (such as `remotesite`), userscripts (e.g., `panda.user.js`), and administrative UI actions. It predates the modern query-dispatch architecture used by the public API (`/api/`) and uses a different request format and error reporting convention. It is currently a work-in-progress (WIP) and slated for future refactoring and migration into the unified dispatch system.

---

## Routes & Endpoints

The `json_parser` view is registered under the following URL patterns in [`viewer/urls.py`](file:///home/netto/.t3/worktrees/pandagallery/t3-a5ffffb0/viewer/urls.py):

| Route | Route Name | Notes |
| :--- | :--- | :--- |
| `/jsonapi/` | `viewer:jsonapi` | Primary internal endpoint (canonical with trailing slash) |
| `/jsonapi` | `viewer:jsonapi-slashless` | Slashless alias for backwards compatibility |
| `/admin-api/` | `viewer:json-parser` | Admin endpoint alias, used by userscripts and web frontend |

> [!NOTE]
> Canonical URLs in Django use trailing slashes (`/jsonapi/`, `/admin-api/`). While `/jsonapi` without trailing slash is supported via `re_path`, callers should prefer `/jsonapi/` or `/admin-api/`.

---

## Authentication & Authorization

Unlike the public `/api/` endpoints (which allow unauthenticated access to public galleries and archives), the internal operations API **strictly enforces authentication and permission checks** for all requests:

1. **Authentication Methods**:
   - **Session Cookie**: Standard Django session (e.g. logged-in user in the web UI).
   - **API Bearer Token**: Sent in the HTTP `Authorization` header:
     ```http
     Authorization: Bearer YOUR_API_TOKEN
     ```
     Validated against active, non-expired tokens in `UserLongLivedToken` that have the `remote` scope.

2. **Required Permission**:
   - The authenticated user must hold the `viewer.use_remote_api` Django permission (`user.has_perm("viewer.use_remote_api")`).

3. **Unauthorized Responses**:
   - If authentication fails or the user lacks `viewer.use_remote_api`, the API returns HTTP `401 Unauthorized`:
     ```json
     {
       "error": "Not authorized"
     }
     ```
   *(Note: This differs from `/api/`, which returns HTTP `403 Forbidden` with `{"result": "Not authorized"}`).*

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
- **Empty request body**:
  ```json
  {"error": "Empty request"}
  ```
- **Missing `operation` or `args`**:
  ```json
  {"error": "Wrong format"}
  ```
- **Unrecognized operation**:
  ```json
  {"error": "Unknown function"}
  ```

### GET Requests (Query Parameter Style)

HTTP `GET` requests are used exclusively for crawler-oriented gallery search via the `gc` query parameter. If `gc` is not provided on a `GET` request, the API returns:
```json
{"error": "Unknown function"}
```

### Unsupported HTTP Methods

If any HTTP method other than `GET` or `POST` is used (e.g. `PUT`, `DELETE`), the endpoint returns HTTP 200 with:
```json
{"error": "Unsupported method: <METHOD>"}
```

---

## Operations Overview

| Operation / Command | Method | Target / Consumer | Description |
| :--- | :--- | :--- | :--- |
| `webcrawler` | `POST` | Internal UI, Userscripts | Queue URLs to download, check existing archives, handle parent replacement |
| `archive_request` | `POST` | `remotesite` command | Query server for existing archives given a list of `[gid, provider]` pairs |
| `force_queue_archives` | `POST` | `remotesite` command | Force-queue URLs for download with fake downloader |
| `queue_archives` | `POST` | `remotesite` command | Deduplicate and queue URLs for archive download |
| `queue_galleries` | `POST` | `remotesite` command | Deduplicate and queue URLs for metadata-only scraping |
| `links` | `POST` | Internal tools | Direct queueing of URL list without existence pre-checks |
| `match_archive` | `POST` | Archive detail UI | Trigger background web matching for an archive |
| `match_archive_internally` | `POST` | Archive detail UI | Trigger local title similarity matching for an archive |
| `?gc=` | `GET` | Sync tools / Crawlers | Filter galleries with crawler metadata |

---

## 1. Web Crawler Enqueueing (`operation: "webcrawler"`)

Queues one or more gallery URLs to the Celery worker task (`task_type="web_crawler"`). This operation supports parent gallery deletion/replacement, downloader filtering, reason tracking, and global filter overrides.

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
curl -X POST "https://example.chaika.moe/jsonapi/" \
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
  "result": "2"
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
```json
{
  "error": "No links provided"
}
```

---

## 2. Remote Archive Verification (`operation: "archive_request"`)

Queries whether specific galleries exist on the server as valid, on-disk archives. Used by the `remotesite` management command to synchronize archives between multiple Panda Backup instances.

- **Method**: `POST`
- **Operation**: `"archive_request"`
- **`args`**: Array of `[gid, provider]` pairs (`list[list[str]]`).

### Example Request
```bash
curl -X POST "https://example.chaika.moe/jsonapi/" \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "operation": "archive_request",
    "args": [
      ["123456", "panda"],
      ["789012", "panda"],
      ["345678", "exhentai"]
    ]
  }'
```

### Response
Returns an array of matching archive records that actually exist on disk (`filter_non_existent`):
```json
{
  "result": [
    {
      "gid": "123456",
      "provider": "panda",
      "id": 42,
      "zipped": "media/archives/42.zip",
      "filesize": 84521033
    }
  ]
}
```

---

## 3. Force Queue Archives (`operation: "force_queue_archives"`)

Forces the crawler worker to download archives for a list of URLs using the `"fake"` downloader type and origin `ORIGIN_ADD_URL`. This bypasses standard duplicate or queue rejection checks.

- **Method**: `POST`
- **Operation**: `"force_queue_archives"`
- **`args`**: Array of gallery URLs (`list[str]`).
- **Optional Root Fields**:
  - `archive_reason` (`str`): Reason metadata for created archives.
  - `archive_details` (`str`): Detailed import description.
  - `ignore_global_filters` / `ignore-global-filters` (`bool`): Set `true` to bypass global crawler filter rules.

### Example Request
```bash
curl -X POST "https://example.chaika.moe/jsonapi/" \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "operation": "force_queue_archives",
    "args": [
      "https://e-hentai.org/g/200001/token1/",
      "https://e-hentai.org/g/200002/token2/"
    ],
    "archive_reason": "Remote instance sync",
    "archive_details": "Synced from server B",
    "ignore_global_filters": true
  }'
```

### Response
```json
{
  "result": "2"
}
```

---

## 4. Queue Archives with Deduplication (`operation: "queue_archives"`)

Parses URLs through configured provider parsers, deletes stale/failed gallery records that lack archives, excludes URLs whose GIDs are already present in the database, and queues only brand-new archives to the worker with the `"fake"` downloader type.

- **Method**: `POST`
- **Operation**: `"queue_archives"`
- **`args`**: Array of gallery URLs (`list[str]`).
- **Optional Root Fields**:
  - `archive_reason` (`str`)
  - `archive_details` (`str`)
  - `ignore_global_filters` (`bool`)

### Cleanup & Filtering Logic
1. Extracts GIDs from URLs using each provider's `id_from_url`.
2. Existing galleries in the database:
   - If submitted (`is_submitted()`): deleted.
   - If marked failed and lacking archives: deleted.
   - If normal and lacking archives: deleted.
3. Excludes any GID already remaining in the database.
4. Enqueues the remaining unique new URLs.

### Example Request
```bash
curl -X POST "https://example.chaika.moe/jsonapi/" \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "operation": "queue_archives",
    "args": [
      "https://e-hentai.org/g/300001/token1/",
      "https://e-hentai.org/g/300002/token2/"
    ],
    "archive_reason": "Batch sync"
  }'
```

### Response
Returns the count of newly queued links (excluding duplicates):
```json
{
  "result": "1"
}
```

---

## 5. Queue Galleries for Metadata Scraping (`operation: "queue_galleries"`)

Similar to `queue_archives`, but sets the downloader mode to `"info"`. This scrapes only gallery metadata, tags, and uploader information without downloading images or creating zip archives.

- **Method**: `POST`
- **Operation**: `"queue_galleries"`
- **`args`**: Array of gallery URLs (`list[str]`).
- **Optional Root Fields**:
  - `ignore_global_filters` (`bool`)

### Example Request
```bash
curl -X POST "https://example.chaika.moe/jsonapi/" \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "operation": "queue_galleries",
    "args": [
      "https://e-hentai.org/g/400001/token1/"
    ]
  }'
```

### Response
```json
{
  "result": "1"
}
```

---

## 6. Raw Link Enqueueing (`operation: "links"`)

Directly enqueues raw links to the `web_crawler` worker task without GID parsing or deduplication checks.

- **Method**: `POST`
- **Operation**: `"links"`
- **`args`**: Array of URL strings (`list[str]`).
- **Optional Root Fields**:
  - `ignore_global_filters` (`bool`)

### Example Request
```bash
curl -X POST "https://example.chaika.moe/jsonapi/" \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "operation": "links",
    "args": [
      "https://e-hentai.org/g/500001/token1/",
      "https://e-hentai.org/g/500002/token2/"
    ]
  }'
```

### Response
```json
{
  "result": "2"
}
```

---

## 7. Web Archive Matcher (`operation: "match_archive"`)

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
curl -X POST "https://example.chaika.moe/jsonapi/" \
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

## 8. Internal Archive Matcher (`operation: "match_archive_internally"`)

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

## 9. Gallery Crawler Search (`GET ?gc=`)

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

## Comparison: `json_parser` vs Modern Public API (`json_api`)

| Feature | `json_parser` (`/jsonapi/`, `/admin-api/`) | `json_api` (`/api/`, `/jsearch/`) |
| :--- | :--- | :--- |
| **Intended Audience** | Internal operations, userscripts, `remotesite` command | Public consumers, third-party apps, external clients |
| **Status** | Legacy / Work In Progress (WIP), planned for migration | Stable, canonical API |
| **Dispatch Style** | JSON body `{"operation": "...", "args": ...}` on `POST`; query param `?gc=` on `GET` | Query parameter dispatch on `GET`, `POST`, `PUT`, `DELETE` (`?gid=`, `?archive=`, etc.) |
| **Authentication** | Strictly required (`viewer.use_remote_api` permission mandatory) | Optional for read (filters `public=true`), required for writes & private data |
| **Auth Failure Code** | `401 Unauthorized` with `{"error": "Not authorized"}` | `403 Forbidden` with `{"result": "Not authorized"}` |
| **Error Format** | `{"error": "<message>"}` | `{"result": "<message>"}` |
| **Supported Methods** | `POST`, `GET` | `GET`, `POST`, `PUT`, `DELETE` |

---

## Migration Roadmap

Future refactoring will migrate operations currently implemented in `json_parser` into the unified handler architecture under `viewer/views/api/`:
1. **Worker/Crawler Queueing**: Migrate `webcrawler`, `links`, `queue_archives`, `queue_galleries`, and `force_queue_archives` into dedicated POST handlers under `viewer/views/api/handlers/`.
2. **Archive Matching**: Standardize `match_archive` and `match_archive_internally` under `/api/?match-archive=...` with consistent permission checking and error envelopes (`{"result": ...}`).
3. **Remote Sync**: Transition `archive_request` to an authenticated `/api/?archive-sync=` or equivalent bulk endpoint.
4. **URL Compatibility**: Maintain `/jsonapi/` and `/admin-api/` routes as legacy proxies redirecting or delegating to the unified handlers until all internal consumers (e.g. `panda.user.js` and `remotesite.py`) are updated.
