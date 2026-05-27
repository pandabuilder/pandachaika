# Wanted Gallery API Documentation

This document describes how to use the API to read and create `WantedGallery` entries.

All endpoints use the public JSON API at `/api`. Authentication is optional for GET requests: unauthenticated clients only receive entries with `public=true`. Authenticated users (session or API token) can also access non-public entries. Missing or inaccessible entries return `404` with `"WantedGallery does not exist."` (same behavior as archives and galleries).

## Get Single Wanted Gallery

**Endpoint**: `/api`  
**Method**: `GET`  
**Query Parameters**: `?wanted-gallery=<id>`

Optional: add `include_found_galleries` (no value required) to embed linked galleries from the `found_galleries` relation in the response.

Returns one WantedGallery object.

### Example Request

```bash
curl "https://example.chaika.moe/api?wanted-gallery=123"
```

With authentication (to access non-public entries):

```bash
curl "https://example.chaika.moe/api?wanted-gallery=123" \
  -H "Authorization: Bearer YOUR_API_TOKEN"
```

With found galleries included:

```bash
curl "https://example.chaika.moe/api?wanted-gallery=123&include_found_galleries"
```

### Response

**Success (200 OK)**

Returns a JSON object with the fields listed in [Response fields](#response-fields).

**Error (404 Not Found)**

Returned if the ID is invalid, the entry does not exist, or it is not public and the caller is not authenticated.

```json
{
  "result": "WantedGallery does not exist."
}
```

## Get Multiple Wanted Galleries by ID

**Endpoint**: `/api`  
**Method**: `GET`  
**Query Parameters**: `?wanted-galleries=<id>&wanted-galleries=<id>...`

Optional: add `include_found_galleries` (no value required) to embed linked galleries on each returned entry.

Returns a JSON array of WantedGallery objects. Repeat the `wanted-galleries` parameter for each ID. Non-public entries are omitted for unauthenticated callers (they are not included in the array rather than causing an error).

### Example Request

```bash
curl "https://example.chaika.moe/api?wanted-galleries=123&wanted-galleries=456"
```

With found galleries on each entry:

```bash
curl "https://example.chaika.moe/api?wanted-galleries=123&wanted-galleries=456&include_found_galleries"
```

### Response

**Success (200 OK)**

```json
[
  { "id": 123, "title": "...", "public": true },
  { "id": 456, "title": "...", "public": true }
]
```

Returns `[]` if no IDs are provided.

**Error (400-style message in body)**

Invalid non-integer IDs:

```json
{
  "result": "Invalid WantedGallery ID."
}
```

## Search Wanted Galleries

**Endpoint**: `/api`  
**Method**: `GET`  
**Query Parameters**: `?wanted-galleries-search=` plus optional filters below.

Returns a JSON array of WantedGallery objects matching the filters. Unauthenticated requests are limited to `public=true` entries.

### Filter parameters

| Parameter | Description |
| :--- | :--- |
| `title` | Substring match on title, Japanese title, search title, or unwanted title |
| `wanted_page_count_lower` | Minimum wanted page count lower bound |
| `wanted_page_count_upper` | Maximum wanted page count upper bound |
| `provider` | Filter by wanted provider slug |
| `not_used` | Entries with no linked archive |
| `wanted-should-search` | `should_search=true` |
| `wanted-should-search-not` | `should_search=false` |
| `book_type` | Book type |
| `publisher` | Publisher |
| `wanted-found` | `found=true` |
| `wanted-not-found` | `found=false` |
| `reason` | Reason |
| `wanted-no-found-galleries` | No linked found galleries |
| `with-possible-matches` | Has possible matches |
| `tags` | Tag filter (comma-separated, same syntax as gallery/archive search) |
| `mention-source` | Mention source |
| `restricted-to-links` | `restricted_to_links=true` |
| `sort` | One of: `title`, `date_found`, `create_date`, `last_modified`, `release_date` |
| `asc_desc` | `desc` for descending order (default is ascending) |

### Example Request

```bash
curl "https://example.chaika.moe/api?wanted-galleries-search=&title=Example&wanted-not-found=1&sort=release_date&asc_desc=desc"
```

## Create Wanted Gallery

**Endpoint**: `/api`  
**Method**: `POST`  
**Query Parameters**: `?wanted-gallery=`  
**Permissions Required**: User must be authenticated and have `viewer.add_wantedgallery` permission.

### Request Body

The request body should be a JSON object with the following fields:

| Field | Type | Description                                                                                     |
| :--- | :--- |:------------------------------------------------------------------------------------------------|
| `title` | `string` | Title of the gallery.                                                                           |
| `title_jpn` | `string` | Japanese title of the gallery.                                                                  |
| `search_title` | `string` | Title used for searching.                                                                       |
| `regexp_search_title` | `boolean` | Treat search title as a regular expression.                                                     |
| `regexp_search_title_icase` | `boolean` | Case-insensitive regex search.                                                                  |
| `unwanted_title` | `string` | Title to exclude/filter out.                                                                    |
| `regexp_unwanted_title` | `boolean` | Treat unwanted title as a regular expression.                                                   |
| `regexp_unwanted_title_icase` | `boolean` | Case-insensitive regex unwanted title.                                                          |
| `wanted_page_count_lower` | `integer` | Minimum page count.                                                                             |
| `wanted_page_count_upper` | `integer` | Maximum page count.                                                                             |
| `match_expression` | `string` | Expression to match against the gallery (ElasticSearch syntax, ES integration required).        |
| `wanted_tags_exclusive_scope` | `boolean` | Enforce exclusive scope for wanted tags.                                                        |
| `exclusive_scope_name` | `string` | Name of the exclusive scope.                                                                    |
| `wanted_tags_accept_if_none_scope` | `string` | Accept if no tags in this scope are present.                                                    |
| `category` | `string` | Main category of the gallery.                                                                   |
| `wait_for_time` | `string/duration` | Duration key or time string (e.g., ISO format or duration string if supported).                 |
| `should_search` | `boolean` | Enable active searching for this gallery.                                                       |
| `keep_searching` | `boolean` | Continue searching even after finding a match.                                                  |
| `reason` | `string` | Reason for wanting this gallery.                                                                |
| `book_type` | `string` | Type of book (e.g., Anthology, Tankoubon).                                                      |
| `publisher` | `string` | Publisher name.                                                                                 |
| `page_count` | `integer` | Exact page count.                                                                               |
| `restricted_to_links` | `boolean` | Restrict search to monitored links only.                                                        |
| `release_date` | `string` | Release date (YYYY-MM-DD or ISO 8601).                                                          |
| `add_to_archive_group` | `integer` | ID of an `ArchiveGroup` to add this gallery to.                                                 |
| `wanted_tags` | `array[string]` | List of tags to require (e.g., `["artist:name", "tag"]`). Can also be a comma-separated string. |
| `unwanted_tags` | `array[string]` | List of tags to exclude.                                                                        |
| `wanted_providers` | `array[string]` | List of provider slugs to include (e.g., `["panda"]`).                                          |
| `unwanted_providers` | `array[string]` | List of provider slugs to exclude.                                                              |
| `categories` | `array[string]` | List of additional categories.                                                                  |

### Example Request

#### cURL

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

#### JSON File

Save the following as `data.json`:

```json
{
  "title": "Example Manga Vol. 1",
  "title_jpn": "Example Manga Vol. 1 JPN",
  "search_title": "Example Manga",
  "should_search": true,
  "wanted_tags": ["artist:example_artist", "female:nekomimi"],
  "unwanted_tags": ["male:ugly_bastard"],
  "category": "Manga",
  "wanted_providers": ["panda", "nhentai"],
  "reason": "Collection completion"
}
```

Then run:

```bash
curl -X POST "https://example.chaika.moe/api?wanted-gallery=" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -d @data.json
```

### Response

**Success (200 OK)**

```json
{
  "result": "success",
  "id": 123
}
```

**Error (404 Not Found)**

Returned if permissions are missing or an error occurs during creation.

```json
{
  "result": "Error creating WantedGallery: <error_message>"
}
```

## Response fields

GET endpoints return objects with these fields:

| Field | Type | Description |
| :--- | :--- | :--- |
| `id` | `integer` | Primary key |
| `title` | `string` | Title |
| `title_jpn` | `string` | Japanese title |
| `search_title` | `string` | Search title |
| `regexp_search_title` | `boolean` | Regex search title |
| `regexp_search_title_icase` | `boolean` | Case-insensitive regex search title |
| `unwanted_title` | `string` | Unwanted title |
| `regexp_unwanted_title` | `boolean` | Regex unwanted title |
| `regexp_unwanted_title_icase` | `boolean` | Case-insensitive regex unwanted title |
| `wanted_page_count_lower` | `integer` | Minimum page count |
| `wanted_page_count_upper` | `integer` | Maximum page count |
| `match_expression` | `string` | Match expression |
| `wanted_tags_exclusive_scope` | `boolean` | Exclusive scope for wanted tags |
| `exclusive_scope_name` | `string` | Exclusive scope name |
| `wanted_tags_accept_if_none_scope` | `string` | Accept-if-none scope |
| `category` | `string` | Category |
| `wait_for_time` | `number` or `null` | Wait duration in seconds |
| `should_search` | `boolean` | Active search enabled |
| `keep_searching` | `boolean` | Keep searching after found |
| `reason` | `string` | Reason |
| `book_type` | `string` | Book type |
| `publisher` | `string` | Publisher |
| `page_count` | `integer` | Page count |
| `restricted_to_links` | `boolean` | Restricted to monitored links |
| `release_date` | `integer` or `null` | Unix timestamp |
| `add_to_archive_group` | `integer` or `null` | Archive group ID |
| `wanted_tags` | `array[string]` | Required tags |
| `unwanted_tags` | `array[string]` | Excluded tags |
| `wanted_providers` | `array[string]` | Provider slugs |
| `unwanted_providers` | `array[string]` | Excluded provider slugs |
| `categories` | `array[string]` | Categories |
| `public` | `boolean` | Visible to unauthenticated API clients |
| `found` | `boolean` | Match found |
| `date_found` | `integer` or `null` | Unix timestamp when found |
| `create_date` | `integer` or `null` | Unix timestamp |
| `last_modified` | `integer` or `null` | Unix timestamp |

When `include_found_galleries` is present, a `found_galleries` array is added. Each element is a gallery linked through `FoundGallery`, with these extra fields from that link: `match_accuracy`, `source`, `found_create_date`. Only galleries with `public=true` are included for unauthenticated callers; authenticated callers receive all linked galleries.
