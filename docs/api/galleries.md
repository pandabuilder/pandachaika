# Gallery Endpoints API Documentation

This document describes all API endpoints for querying and searching `Gallery` records in Panda Backup.

All endpoints use the public JSON API at `/api` (or `/jsearch`).

---

## Authentication & Access Control

- **Unauthenticated requests**: Callers can only view galleries with `public=true`. If an unauthenticated caller requests a gallery that is not public, `404 Not Found` is returned. In search and multi-lookup endpoints, non-public galleries are omitted from results.
- **Authenticated requests**: Passing an `Authorization: Bearer <API_TOKEN>` header or an active Django session allows access to both public and non-public galleries.
- **Embedded Archives**: When archives are returned within a gallery response, unauthenticated requests only receive archives where `public=true`. Authenticated requests receive all available archives for that gallery.

---

## Endpoint Overview

| Command Parameter | Method | Description |
| :--- | :--- | :--- |
| `?gid=<gid>` | `GET` | Single gallery lookup by provider gallery ID |
| `?gids=<gid>&gids=<gid>...` | `GET` | Multiple galleries lookup by provider gallery ID |
| `?gallery=<id>` | `GET` | Single gallery lookup by internal database primary key |
| `?gd=<id>` | `GET` | Detailed single gallery with archive download links, sources, and reasons |
| `?gt=<id>` | `GET` | Sorted list of tags for a gallery |
| `?g=` | `GET` | Filtered gallery search (compact attributes) |
| `?gc=` | `GET` | Filtered gallery search for crawlers/syncing |
| `?gs=` | `GET` | Filtered gallery search with downloadable archives (unpaginated) |
| `?gsp=` | `GET` | Paginated gallery search with downloadable archives |
| `?match=` | `GET` | Filtered gallery search for matching tools (includes container/parent relations) |

---

## 1. Single Gallery by Provider GID (`gid`)

Fetches a single gallery by the ID assigned to it on the upstream provider site (e.g. Panda/ExHentai numeric ID).

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**:

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `gid` | `string` / `integer` | Yes | Gallery ID on the provider site. |
| `provider` | `string` | No | Provider slug (e.g. `panda`, `nhentai`). Restricts the lookup to that provider. If omitted, returns the first matching gallery. |

### Example Request
```bash
curl "https://example.chaika.moe/api?gid=1234567&provider=panda"
```

With authentication:
```bash
curl "https://example.chaika.moe/api?gid=1234567&provider=panda" \
  -H "Authorization: Bearer YOUR_API_TOKEN"
```

### Response
**Success (200 OK)**
```json
{
  "gid": "1234567",
  "title": "Example Title",
  "title_jpn": "Example Japanese Title",
  "category": "Non-H",
  "uploader": "UploaderName",
  "posted": 1546300800,
  "filecount": 24,
  "filesize": 12453880,
  "expunged": false,
  "disowned": false,
  "rating": 4.5,
  "fjord": false,
  "link": "https://exhentai.org/g/1234567/abcdef/",
  "tags": [
    "artist:sample",
    "female:schoolgirl",
    "language:english"
  ],
  "archives": [
    {
      "id": 42,
      "download": "/archive/42/download/"
    }
  ]
}
```

**Error (404 Not Found)**  
Returned if the gallery does not exist or is not public for an unauthenticated request:
```json
{
  "result": "Gallery does not exist."
}
```

---

## 2. Multiple Galleries by Provider GID (`gids`)

Fetches multiple galleries by their provider IDs in a single request.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**:

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `gids` | `string` / `integer` | Yes (repeatable) | One or more provider gallery IDs: `gids=123&gids=456`. |
| `provider` | `string` | No | Provider slug applied to all IDs in the request. |

### Example Request
```bash
curl "https://example.chaika.moe/api?gids=1234567&gids=7654321&provider=panda"
```

### Response
**Success (200 OK)**  
Returns a JSON array of gallery objects (same schema as `gid`). GIDs that do not exist or are inaccessible are silently omitted:
```json
[
  {
    "gid": "1234567",
    "title": "Example Title 1",
    "link": "https://...",
    "tags": ["language:english"],
    "archives": []
  },
  {
    "gid": "7654321",
    "title": "Example Title 2",
    "link": "https://...",
    "tags": ["female:nekomimi"],
    "archives": [{"id": 42, "download": "/archive/42/download/"}]
  }
]
```

---

## 3. Single Gallery by Internal ID (`gallery`)

Fetches a single gallery using its internal database primary key.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?gallery=<id>`

### Example Request
```bash
curl "https://example.chaika.moe/api?gallery=105"
```

### Response
Returns the standard gallery object (same schema as `gid`). Returns `404 Not Found` with `{"result": "Gallery does not exist."}` if invalid or inaccessible.

---

## 4. Detailed Gallery by Internal ID (`gd`)

Fetches detailed gallery information including full archive records with download URLs, source types, and crawler reasons.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?gd=<id>`

### Example Request
```bash
curl "https://example.chaika.moe/api?gd=105"
```

### Response
**Success (200 OK)**
```json
{
  "id": 105,
  "gid": "1234567",
  "token": "a1b2c3d4e5",
  "title": "Detailed Example Title",
  "title_jpn": "Japanese Title",
  "category": "Manga",
  "uploader": "UploaderName",
  "comment": "Uploader comments",
  "posted": 1546300800,
  "filecount": 48,
  "filesize": 25482910,
  "expunged": false,
  "disowned": false,
  "provider": "panda",
  "rating": "4.8",
  "fjord": false,
  "tags": ["artist:example"],
  "link": "https://exhentai.org/g/1234567/a1b2c3d4e5/",
  "thumbnail": "https://example.chaika.moe/gallery/105/thumb/",
  "thumbnail_url": "https://ehgt.org/...",
  "archives": [
    {
      "link": "https://example.chaika.moe/archive/42/download/",
      "source": "web",
      "reason": "Direct user download"
    }
  ]
}
```

---

## 5. Gallery Tags (`gt`)

Fetches the complete sorted list of tags associated with a gallery.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?gt=<id>`

### Example Request
```bash
curl "https://example.chaika.moe/api?gt=105"
```

### Response
```json
{
  "tags": [
    "artist:sample_artist",
    "female:schoolgirl",
    "language:english",
    "language:translated"
  ]
}
```

---

## 6. Filtered Gallery Search (`g`, `gc`, `gs`, `gsp`, `match`)

Panda Backup offers several endpoints for searching galleries based on metadata and tags. All search endpoints accept the parameters listed in [Gallery Search Filter Parameters](#gallery-search-filter-parameters).

### 6.1 Minimal Gallery Search (`g`)
Returns a lightweight array of gallery records with essential metadata:
- **Request**: `GET /api?g=&title=Example&rating_from=4.0`
- **Response Fields**: `title`, `title_jpn`, `category`, `uploader`, `posted`, `filecount`, `filesize`, `expunged`, `disowned`, `source`, `rating`, `fjord`, `tags`.

### 6.2 Crawler Search (`gc`)
Used by crawler scripts and sync tools to export galleries:
- **Request**: `GET /api?gc=&provider=panda&sort=posted&asc_desc=desc`
- **Response Fields**: Includes `gid`, `token`, `provider`, and `link` in addition to metadata.

### 6.3 Full Gallery Search with Archives (`gs`)
Returns full gallery details and all available archives (unpaginated):
- **Request**: `GET /api?gs=&tags=female:nekomimi&used=1`
- **Response Fields**: Each gallery includes an `archives` array with `link`, `source`, and `reason`.

### 6.4 Paginated Gallery Search (`gsp`)
Recommended for web interfaces and large queries. Supports standard pagination:
- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**:
  - `gsp`: (flag) Triggers paginated search.
  - `page`: (integer, default `1`) Current page number.
  - `count`: (integer, default `48`) Items per page. Allowed values: `24`, `48`, `100`, `200`, `300`.
  - Additional filters from [Gallery Search Filter Parameters](#gallery-search-filter-parameters).

#### Example Request
```bash
curl "https://example.chaika.moe/api?gsp=&page=2&count=48&tags=artist:example&sort=posted&asc_desc=desc"
```

#### Response
```json
{
  "galleries": [
    {
      "id": 105,
      "gid": "1234567",
      "token": "abcdef1234",
      "title": "Example Gallery",
      "title_jpn": "Example Japanese",
      "category": "Manga",
      "uploader": "UploaderName",
      "comment": "",
      "posted": 1546300800,
      "filecount": 32,
      "filesize": 18450123,
      "expunged": false,
      "disowned": false,
      "provider": "panda",
      "rating": "4.5",
      "fjord": false,
      "tags": ["artist:example"],
      "link": "https://exhentai.org/g/1234567/abcdef1234/",
      "url": "https://example.chaika.moe/gallery/105/",
      "thumbnail": "https://example.chaika.moe/gallery/105/thumb/",
      "thumbnail_url": "https://ehgt.org/...",
      "archives": [
        {
          "link": "https://example.chaika.moe/archive/42/download/",
          "source": "web",
          "reason": "Queue download"
        }
      ]
    }
  ],
  "has_previous": true,
  "has_next": true,
  "num_pages": 5,
  "count": 220,
  "number": 2
}
```

### 6.5 Matcher Search (`match`)
Used by internal and external matchers. Returns relational identifiers:
- **Request**: `GET /api?match=&crc32=A1B2C3D4`
- **Additional Response Fields**: `gallery_container`, `parent_gid`, `first_gid`, `magazine`, `thumbnail`, `thumbnail_url`.

---

## Gallery Search Filter Parameters

These query parameters can be passed to `g`, `gc`, `gs`, `gsp`, and `match`:

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `title` | `string` | Case-insensitive substring match on `title` or `title_jpn` (supports spaces). |
| `tags` | `string` | Comma-separated tag filter (e.g. `artist:name,female:big_breasts`). Prefix with `-` to exclude, `^` for exact match, `-^` for negated exact. |
| `category` | `string` | Substring match on category (e.g. `Manga`, `Doujinshi`). |
| `provider` | `string` | Filter by provider slug (e.g. `panda`, `nhentai`). |
| `uploader` | `string` | Exact match on uploader username. |
| `rating_from` | `float` | Minimum rating (inclusive). |
| `rating_to` | `float` | Maximum rating (inclusive). |
| `filecount_from` | `integer` | Minimum page / file count (inclusive). |
| `filecount_to` | `integer` | Maximum page / file count (inclusive). |
| `filesize_from` | `integer` | Minimum size in bytes (inclusive). |
| `filesize_to` | `integer` | Maximum size in bytes (inclusive). |
| `posted_from` | `integer` | Minimum posted date as Unix timestamp. |
| `posted_to` | `integer` | Maximum posted date as Unix timestamp. |
| `create_from` | `integer` | Minimum gallery creation date in database (Unix timestamp). |
| `create_to` | `integer` | Maximum gallery creation date in database (Unix timestamp). |
| `dl_type` | `string` | Download type / status filter. |
| `reason` | `string` | Substring match on reason. |
| `crc32` | `string` | Filter by linked archive CRC32 checksum. |
| `used` | `boolean` | When set to `1`, only returns galleries that have at least one associated archive or container archive. |
| `expunged` | `boolean` | When set to `1`, filter for expunged galleries. |
| `disowned` | `boolean` | When set to `1`, filter for disowned galleries. |
| `hidden` | `boolean` | When set to `1`, filter for hidden galleries. |
| `fjord` | `boolean` | When set to `1`, filter for fjord-marked galleries. |
| `sort` | `string` | Sort field: `title`, `title_jpn`, `rating`, `filesize`, `filecount`, `posted`, `create_date`, `category`, `provider`, `uploader`. Default is `posted`. |
| `asc_desc` | `string` | `desc` for descending order, `asc` for ascending order. |

---

## Response Field Definitions

| Field | Type | Description |
| :--- | :--- | :--- |
| `id` | `integer` | Internal database primary key |
| `gid` | `string` | Provider gallery ID |
| `token` | `string` | Provider gallery token |
| `title` | `string` | Primary gallery title |
| `title_jpn` | `string` | Japanese title |
| `category` | `string` | Main category (e.g. Manga, Doujinshi, Non-H) |
| `uploader` | `string` | Uploader username |
| `comment` | `string` | Gallery description / uploader comment |
| `posted` | `integer` | Original upload date as Unix timestamp |
| `filecount` | `integer` | Total number of pages / files |
| `filesize` | `integer` | Total filesize in bytes |
| `expunged` | `boolean` | Indicates if the gallery was deleted/expunged on the provider site |
| `disowned` | `boolean` | Indicates if the gallery was disowned by its uploader |
| `rating` | `float` / `string` | Average user rating (0.0 to 5.0) |
| `fjord` | `boolean` | Internal flagging status |
| `provider` / `source` | `string` | Provider slug |
| `link` | `string` | URL to the original gallery on the provider site |
| `url` | `string` | Absolute URL to the gallery view on this Panda Backup instance |
| `thumbnail` | `string` | Absolute URL to local cached thumbnail |
| `thumbnail_url` | `string` | Remote provider thumbnail URL |
| `tags` | `array[string]` | List of tags formatted as `scope:name` or `name` |
| `archives` | `array[object]` | Downloadable archives associated with this gallery |
