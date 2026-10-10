# Wanted Gallery Endpoints API Documentation

This document describes all API endpoints for reading, searching, and creating `WantedGallery` records in Panda Backup.

All endpoints use the public JSON API at `/api` (or `/jsearch`).

---

## Authentication & Access Control

- **Unauthenticated GET requests**: Unauthenticated callers only receive entries where `public=true`. If an unauthenticated caller requests a non-public entry, `404 Not Found` with `{"result": "WantedGallery does not exist."}` is returned.
- **Authenticated GET requests**: Passing an `Authorization: Bearer <API_TOKEN>` header or active Django session allows access to both public and non-public wanted galleries.
- **Creating Wanted Galleries (`POST`)**: Requires an authenticated user with the `viewer.add_wantedgallery` permission. Unauthenticated or unauthorized callers receive `403 Forbidden` (`{"result": "Not authorized"}`).

---

## Endpoint Overview

| Command Parameter | Method | Permissions | Description |
| :--- | :--- | :--- | :--- |
| `?wanted-gallery=<id>` | `GET` | None | Get a single WantedGallery object |
| `?wanted-galleries=<id>&wanted-galleries=<id>...` | `GET` | None | Get multiple WantedGallery objects |
| `?wanted-galleries-search=` | `GET` | None | Search WantedGallery objects with filters |
| `?wanted-gallery=` | `POST` | `viewer.add_wantedgallery` | Create a new WantedGallery |

---

## 1. Get Single Wanted Gallery (`wanted-gallery`)

Fetches a single WantedGallery entry by its primary key.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**:

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `wanted-gallery` | `integer` | Yes | Primary key of the WantedGallery. |
| `include_found_galleries` | `flag` | No | If present (no value needed), embeds linked `found_galleries` in the response. |

### Example Request
```bash
curl "https://example.chaika.moe/api?wanted-gallery=123"
```

With authentication and linked found galleries:
```bash
curl "https://example.chaika.moe/api?wanted-gallery=123&include_found_galleries" \
  -H "Authorization: Bearer YOUR_API_TOKEN"
```

### Response
**Success (200 OK)**  
Returns a JSON object with the fields listed in [Response Field Definitions](#response-field-definitions).

```json
{
  "id": 123,
  "title": "Example Manga Vol. 1",
  "title_jpn": "Example Manga Vol. 1 JPN",
  "search_title": "Example Manga",
  "regexp_search_title": false,
  "regexp_search_title_icase": false,
  "unwanted_title": "",
  "regexp_unwanted_title": false,
  "regexp_unwanted_title_icase": false,
  "wanted_page_count_lower": 0,
  "wanted_page_count_upper": 0,
  "match_expression": null,
  "wanted_tags_exclusive_scope": false,
  "exclusive_scope_name": "",
  "wanted_tags_accept_if_none_scope": "",
  "category": "Manga",
  "wait_for_time": null,
  "should_search": true,
  "keep_searching": false,
  "reason": "Collection completion",
  "book_type": "",
  "publisher": "",
  "page_count": 0,
  "restricted_to_links": false,
  "release_date": null,
  "add_to_archive_group": null,
  "wanted_tags": ["artist:example_artist", "female:nekomimi"],
  "unwanted_tags": ["male:ugly_bastard"],
  "wanted_providers": ["panda", "nhentai"],
  "unwanted_providers": [],
  "categories": ["Manga"],
  "public": true,
  "found": false,
  "date_found": null,
  "create_date": 1600000000,
  "last_modified": 1600000000
}
```

**Error (404 Not Found)**  
Returned if the ID does not exist, or the entry is not public and the caller is unauthenticated:
```json
{
  "result": "WantedGallery does not exist."
}
```

---

## 2. Get Multiple Wanted Galleries (`wanted-galleries`)

Fetches multiple WantedGallery entries in a single request.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**:

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `wanted-galleries` | `integer` | Yes (repeatable) | WantedGallery IDs: `wanted-galleries=123&wanted-galleries=456`. |
| `include_found_galleries` | `flag` | No | If present, embeds linked found galleries in each entry. |

### Example Request
```bash
curl "https://example.chaika.moe/api?wanted-galleries=123&wanted-galleries=456&include_found_galleries"
```

### Response
**Success (200 OK)**  
Returns a JSON array of WantedGallery objects. Inaccessible non-public items are omitted from the array.
```json
[
  { "id": 123, "title": "Example Manga Vol. 1", "public": true },
  { "id": 456, "title": "Example Manga Vol. 2", "public": true }
]
```

**Error (Invalid ID)**  
If non-integer IDs are provided:
```json
{
  "result": "Invalid WantedGallery ID."
}
```

---

## 3. Search Wanted Galleries (`wanted-galleries-search`)

Filters and searches WantedGallery records.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?wanted-galleries-search=` plus filter parameters below.

### Filter Parameters Reference

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `title` | `string` | Substring match on `title`, `title_jpn`, `search_title`, or `unwanted_title`. |
| `wanted_page_count_lower` | `integer` | Minimum wanted page count. |
| `wanted_page_count_upper` | `integer` | Maximum wanted page count. |
| `provider` | `string` | Filter by wanted provider slug. |
| `not_used` | `boolean` | Filter for entries with no linked archive. |
| `wanted-should-search` | `boolean` | Filter for entries where `should_search=true`. |
| `wanted-should-search-not` | `boolean` | Filter for entries where `should_search=false`. |
| `book_type` | `string` | Book type filter. |
| `publisher` | `string` | Publisher filter. |
| `wanted-found` | `boolean` | Filter for entries where `found=true`. |
| `wanted-not-found` | `boolean` | Filter for entries where `found=false`. |
| `reason` | `string` | Reason filter. |
| `wanted-no-found-galleries` | `boolean` | Filter for entries with no linked found galleries. |
| `with-possible-matches` | `boolean` | Has possible matches. |
| `tags` | `string` | Comma-separated tag query (same syntax as gallery/archive search). |
| `mention-source` | `string` | Mention source filter. |
| `restricted-to-links` | `boolean` | Filter for entries restricted to links. |
| `sort` | `string` | Sort field: `title`, `date_found`, `create_date`, `last_modified`, `release_date`. |
| `asc_desc` | `string` | `desc` for descending order, `asc` for ascending. Default is ascending. |

### Example Request
```bash
curl "https://example.chaika.moe/api?wanted-galleries-search=&title=Example&wanted-not-found=1&sort=release_date&asc_desc=desc"
```

---

## 4. Create Wanted Gallery (`POST ?wanted-gallery=`)

Creates a new WantedGallery record.

- **Endpoint**: `/api`
- **Method**: `POST`
- **Query Parameter**: `?wanted-gallery=`
- **Permissions**: Requires an authenticated user with `viewer.add_wantedgallery` permission.
- **Headers**:
  - `Content-Type: application/json`
  - `Authorization: Bearer YOUR_API_TOKEN`

### Request Body Schema

| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `title` | `string` | Yes | Primary title of the gallery to monitor. |
| `title_jpn` | `string` | No | Japanese title. |
| `search_title` | `string` | No | Specific search phrase to use during crawling. |
| `regexp_search_title` | `boolean` | No | Treat `search_title` as a regex pattern. Default `false`. |
| `regexp_search_title_icase` | `boolean` | No | Case-insensitive regex matching. Default `false`. |
| `unwanted_title` | `string` | No | Title pattern to ignore/exclude. |
| `regexp_unwanted_title` | `boolean` | No | Treat `unwanted_title` as regex. Default `false`. |
| `regexp_unwanted_title_icase` | `boolean` | No | Case-insensitive regex unwanted title. Default `false`. |
| `wanted_page_count_lower` | `integer` | No | Minimum acceptable page count. |
| `wanted_page_count_upper` | `integer` | No | Maximum acceptable page count. |
| `match_expression` | `string` | No | Custom query expression (ElasticSearch syntax if ES is enabled). |
| `wanted_tags_exclusive_scope` | `boolean` | No | Enforce exclusive scope for tags. |
| `exclusive_scope_name` | `string` | No | Name of exclusive scope. |
| `wanted_tags_accept_if_none_scope`| `string` | No | Accept match if no tags in this scope are present. |
| `category` | `string` | No | Main gallery category (e.g. `Manga`). |
| `wait_for_time` | `number` / `null` | No | Wait time in seconds before download triggers. |
| `should_search` | `boolean` | No | Enable active crawler searching. Default `false`. |
| `keep_searching` | `boolean` | No | Continue searching after initial match. Default `false`. |
| `reason` | `string` | No | Reason for monitoring this item. |
| `book_type` | `string` | No | Type of publication (e.g. `Tankoubon`, `Anthology`). |
| `publisher` | `string` | No | Publisher name. |
| `page_count` | `integer` | No | Exact page count. |
| `restricted_to_links` | `boolean` | No | Limit searching to monitored links only. |
| `release_date` | `string` | No | Release date (`YYYY-MM-DD` or ISO 8601 string). |
| `add_to_archive_group` | `integer` | No | ArchiveGroup primary key to automatically add matched archives to. |
| `wanted_tags` | `array[string]` | No | List of required tags (e.g. `["artist:alpha", "female:nekomimi"]`). Can also be a comma-separated string. |
| `unwanted_tags` | `array[string]` | No | List of excluded tags. |
| `wanted_providers` | `array[string]` | No | List of provider slugs to search (e.g. `["panda", "nhentai"]`). |
| `unwanted_providers` | `array[string]` | No | List of provider slugs to exclude. |
| `categories` | `array[string]` | No | Additional categories list. |

### Example Request
```bash
curl -X POST "https://example.chaika.moe/api?wanted-gallery=" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -d '{
    "title": "Example Manga Vol. 1",
    "title_jpn": "Example Manga Vol. 1 JPN",
    "search_title": "Example Manga",
    "should_search": true,
    "wanted_tags": ["artist:example_artist", "female:nekomimi"],
    "unwanted_tags": ["male:ugly_bastard"],
    "category": "Manga",
    "wanted_providers": ["panda", "nhentai"],
    "reason": "Collection completion"
  }'
```

### Response
**Success (200 OK)**
```json
{
  "result": "success",
  "id": 123
}
```

**Error (403 Forbidden)**  
Missing authentication or `viewer.add_wantedgallery` permission:
```json
{
  "result": "Not authorized"
}
```

---

## Response Field Definitions

| Field | Type | Description |
| :--- | :--- | :--- |
| `id` | `integer` | Primary key |
| `title` | `string` | Monitored gallery title |
| `title_jpn` | `string` | Japanese title |
| `search_title` | `string` | Crawler search phrase |
| `regexp_search_title` | `boolean` | Search title is regular expression |
| `regexp_search_title_icase` | `boolean` | Case-insensitive search title regex |
| `unwanted_title` | `string` | Excluded title pattern |
| `regexp_unwanted_title` | `boolean` | Unwanted title is regular expression |
| `regexp_unwanted_title_icase` | `boolean` | Case-insensitive unwanted title regex |
| `wanted_page_count_lower` | `integer` | Minimum acceptable pages |
| `wanted_page_count_upper` | `integer` | Maximum acceptable pages |
| `match_expression` | `string` / `null` | Advanced query expression |
| `wanted_tags_exclusive_scope` | `boolean` | Exclusive tag scope flag |
| `exclusive_scope_name` | `string` | Exclusive tag scope name |
| `wanted_tags_accept_if_none_scope` | `string` | Accept-if-none scope name |
| `category` | `string` | Category |
| `wait_for_time` | `number` / `null` | Delay in seconds |
| `should_search` | `boolean` | Active crawler search enabled |
| `keep_searching` | `boolean` | Continue searching after finding a match |
| `reason` | `string` | Monitoring reason |
| `book_type` | `string` | Book type |
| `publisher` | `string` | Publisher |
| `page_count` | `integer` | Expected page count |
| `restricted_to_links` | `boolean` | Monitored link restriction flag |
| `release_date` | `integer` / `null` | Unix timestamp of release date |
| `add_to_archive_group` | `integer` / `null` | Target ArchiveGroup ID |
| `wanted_tags` | `array[string]` | Required tags |
| `unwanted_tags` | `array[string]` | Excluded tags |
| `wanted_providers` | `array[string]` | Monitored provider slugs |
| `unwanted_providers` | `array[string]` | Excluded provider slugs |
| `categories` | `array[string]` | Categories |
| `public` | `boolean` | Visible to unauthenticated API clients |
| `found` | `boolean` | True if at least one matching gallery was found |
| `date_found` | `integer` / `null` | Unix timestamp when match was found |
| `create_date` | `integer` / `null` | Creation timestamp |
| `last_modified` | `integer` / `null` | Last update timestamp |

When `include_found_galleries` is requested, a `found_galleries` array is added. Each element contains:
- Standard gallery fields (`id`, `gid`, `token`, `title`, `title_jpn`, `category`, `uploader`, `posted`, `filecount`, `filesize`, `provider`, `rating`, `tags`, `link`)
- `match_accuracy` (`float`): Match confidence score
- `source` (`string`): Finding method/worker
- `found_create_date` (`integer`): Unix timestamp when match link was created
