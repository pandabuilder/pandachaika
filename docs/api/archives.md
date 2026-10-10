# Archive Endpoints API Documentation

This document describes all API endpoints for querying, inspecting, and searching `Archive` records (zip files) in Panda Backup.

All endpoints use the public JSON API at `/api` (or `/jsearch`).

---

## Authentication & Access Control

- **Unauthenticated requests**: Callers only receive archives marked as `public=true`. Any attempt to access a non-public archive directly returns `404 Not Found` with `{"result": "Archive does not exist."}`. In search endpoints, non-public archives are automatically excluded.
- **Authenticated requests**: Passing an `Authorization: Bearer <API_TOKEN>` header or an active Django session gives access to all archives regardless of public status. Authenticated callers also receive private timestamps (`create_date`).
- **Archive Downloads**: The `download` field provides direct access to download the zip file (`/archive/<id>/download/`). Download permission is governed by the archive's public status and user session.

---

## Endpoint Overview

| Command Parameter | Method | Description |
| :--- | :--- | :--- |
| `?archive=<id>` | `GET` | Single archive details and download URL |
| `?archives=<id>&archives=<id>...` | `GET` | Multiple archives lookup by ID |
| `?at=<id>` | `GET` | Sorted tag list for an archive |
| `?ah=<id>` | `GET` | Array of image SHA-1 hashes for an archive |
| `?aof=<id>` | `GET` | List of non-image extra files inside an archive |
| `?aid=<id>&aid=<id>...` | `GET` | Per-page image metadata (dimensions, hash, format, size) |
| `?sha1=<hash>` | `GET` | Reverse lookup: find archives containing an image with this SHA-1 |
| `?qa=` | `GET` | Filtered archive search returning compact objects |
| `?q=<query>` | `GET` | Simple text/tag search returning compact objects |
| `?as=` | `GET` | Archive search returning associated galleries with archives (sync/download) |
| `?archive-wanted-image=<id>` | `GET` | Image match similarity against active wanted images *(auth required)* |

---

## 1. Get Single Archive (`archive`)

Fetches metadata for a single archive by its primary key.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?archive=<id>`

### Example Request
```bash
curl "https://example.chaika.moe/api?archive=42"
```

With authentication:
```bash
curl "https://example.chaika.moe/api?archive=42" \
  -H "Authorization: Bearer YOUR_API_TOKEN"
```

### Response
**Success (200 OK)**
```json
{
  "title": "Example Archive Title",
  "title_jpn": "Example Japanese Title",
  "category": "Manga",
  "uploader": "UploaderName",
  "posted": 1546300800,
  "filecount": 32,
  "filesize": 18450123,
  "crc32": "A1B2C3D4",
  "expunged": false,
  "disowned": false,
  "rating": 4.5,
  "fjord": false,
  "tags": [
    "artist:sample",
    "female:schoolgirl",
    "language:english"
  ],
  "download": "/archive/42/download/",
  "gallery": 105
}
```

**Error (404 Not Found)**
```json
{
  "result": "Archive does not exist."
}
```

---

## 2. Get Multiple Archives (`archives`)

Fetches full details for multiple archives in a single call.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?archives=<id>&archives=<id>...` (repeatable)

### Example Request
```bash
curl "https://example.chaika.moe/api?archives=42&archives=43"
```

### Response
**Success (200 OK)**  
Returns a JSON array of archive objects:
```json
[
  {
    "id": 42,
    "title": "Example Archive 1",
    "title_jpn": "JPN Title",
    "filecount": 32,
    "filesize": 18450123,
    "posted": 1546300800,
    "public_date": 1550000000,
    "create_date": 1549000000,
    "source": "web",
    "reason": "Queue download",
    "category": "Manga",
    "uploader": "UploaderName",
    "rating": "4.5",
    "link": "https://exhentai.org/g/1234567/abcdef/",
    "download": "https://example.chaika.moe/archive/42/download/",
    "url": "https://example.chaika.moe/archive/42/",
    "thumbnail": "https://example.chaika.moe/media/thumb/42.jpg",
    "tags": ["artist:sample", "female:schoolgirl"]
  }
]
```

*Note: `create_date` is `null` for unauthenticated requests.*

---

## 3. Archive Tags (`at`)

Retrieves the alphabetically sorted list of tags for a specific archive.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?at=<id>`

### Example Request
```bash
curl "https://example.chaika.moe/api?at=42"
```

### Response
```json
{
  "tags": [
    "artist:sample",
    "female:schoolgirl",
    "language:english"
  ]
}
```

---

## 4. Archive Image Hashes (`ah`)

Retrieves the SHA-1 checksums for all images contained within the archive in page order.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?ah=<id>`

### Example Request
```bash
curl "https://example.chaika.moe/api?ah=42"
```

### Response
```json
{
  "image_hashes": [
    "356a192b7913b04c54574d18c28d46e6395428ab",
    "da4b9237bacccdf19c0760cab7aec4a8359010b0",
    "77de68daecd823babbb58edb1c8e14d7106e83bb"
  ]
}
```

---

## 5. Non-Image Files in Archive (`aof`)

Inspects any non-image files stored in the archive (e.g. text files, NFO files, metadata exports).

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?aof=<id>`

### Example Request
```bash
curl "https://example.chaika.moe/api?aof=42"
```

### Response
```json
{
  "other_files": [
    {
      "name": "info.txt",
      "size": 1024,
      "sha1": "da39a3ee5e6b4b0d3255bfef95601890afd80709"
    }
  ]
}
```

---

## 6. Detailed Image Metadata for Archives (`aid`)

Fetches complete page-by-page image technical metadata (dimensions, file formats, colorspace, size, and hash) for one or more archives.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?aid=<id>&aid=<id>...` (repeatable)

### Example Request
```bash
curl "https://example.chaika.moe/api?aid=42"
```

### Response
Returns an object keyed by archive ID, containing objects keyed by page position (1-indexed):
```json
{
  "42": {
    "1": {
      "id": 1001,
      "position": 1,
      "archive_position": 1,
      "filename": "001.jpg",
      "size": 524288,
      "sha1": "356a192b7913b04c54574d18c28d46e6395428ab",
      "height": 1600,
      "width": 1200,
      "format": "JPEG",
      "mode": "RGB"
    },
    "2": {
      "id": 1002,
      "position": 2,
      "archive_position": 2,
      "filename": "002.jpg",
      "size": 612040,
      "sha1": "da4b9237bacccdf19c0760cab7aec4a8359010b0",
      "height": 1600,
      "width": 1200,
      "format": "JPEG",
      "mode": "RGB"
    }
  }
}
```

---

## 7. Search Archives by Image SHA-1 (`sha1`)

Reverse image search: finds all archives in the database that contain an image matching a specific SHA-1 hash.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?sha1=<sha1_hex>`

### Example Request
```bash
curl "https://example.chaika.moe/api?sha1=356a192b7913b04c54574d18c28d46e6395428ab"
```

### Response
Returns an array of archive objects containing the matched image:
```json
[
  {
    "id": 42,
    "title": "Example Archive Title",
    "title_jpn": "Example Japanese Title",
    "category": "Manga",
    "uploader": "UploaderName",
    "posted": 1546300800,
    "filecount": 32,
    "filesize": 18450123,
    "expunged": false,
    "disowned": false,
    "rating": 4.5,
    "fjord": false,
    "tags": ["artist:sample"],
    "download": "/archive/42/download/",
    "gallery": 105
  }
]
```

---

## 8. Filtered Quick Search (`qa`)

Performs a filtered archive search and returns compact archive identifiers and download URLs.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?qa=` plus filters from [Archive Filter Parameters](#archive-filter-parameters).

### Example Request
```bash
curl "https://example.chaika.moe/api?qa=&tags=artist:sample&sort=public_date&asc_desc=desc"
```

### Response
```json
[
  {
    "id": 42,
    "title": "Example Archive Title",
    "tags": ["artist:sample", "language:english"],
    "url": "/archive/42/download/"
  }
]
```

---

## 9. Simple Text Archive Search (`q`)

Simple keyword and tag search over archive titles and tags.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?q=<search_text>`

Supports space-separated title keywords and comma-separated tags (with `-` and `^` prefixes).

### Example Request
```bash
curl "https://example.chaika.moe/api?q=Sample+Vol+1"
```

### Response
Returns the same compact format as `qa`:
```json
[
  {
    "id": 42,
    "title": "Sample Vol 1",
    "tags": ["language:english"],
    "url": "/archive/42/download/"
  }
]
```

---

## 10. Archive Search Grouped by Gallery (`as`)

Searches archives using archive filters, but aggregates the results under their parent `Gallery` records. Designed for transferring and syncing archives between Panda Backup instances.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?as=` plus filters from [Archive Filter Parameters](#archive-filter-parameters).

### Example Request
```bash
curl "https://example.chaika.moe/api?as=&tags=artist:sample"
```

### Response
Returns an array of Gallery objects with all matching archives attached:
```json
[
  {
    "id": 105,
    "gid": "1234567",
    "token": "abcdef1234",
    "title": "Example Gallery",
    "title_jpn": "Example Japanese Title",
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
    "tags": ["artist:sample"],
    "link": "https://exhentai.org/g/1234567/abcdef1234/",
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
]
```

---

## 11. Wanted Image Match Similarity (`archive-wanted-image`)

Runs feature matching and image similarity algorithms between images in this archive and all active `WantedImage` records.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?archive-wanted-image=<id>`
- **Permissions**: Requires an **authenticated user**.

### Example Request
```bash
curl "https://example.chaika.moe/api?archive-wanted-image=42" \
  -H "Authorization: Bearer YOUR_API_TOKEN"
```

### Response
```json
{
  "archive": {
    "id": 42,
    "title": "Example Archive",
    "download": "https://example.chaika.moe/archive/42/download/"
  },
  "matches": [
    {
      "url": "https://example.chaika.moe/wanted-image/10/",
      "name": "Target Illustration",
      "minimum_features": 50,
      "good_matches": 72,
      "found_match": true,
      "found_image": "data:image/jpeg;base64,/9j/4AAQSkZJRg..."
    }
  ]
}
```

---

## Archive Filter Parameters

These query parameters can be passed to `qa` and `as`:

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `title` | `string` | Substring match on archive `title` or `title_jpn`. |
| `filename` | `string` | Substring match on zip filename (`zipped`). |
| `tags` | `string` | Comma-separated tag query (e.g. `artist:name,female:stockings`). Prefix `-` to exclude, `^` for exact match. |
| `category` | `string` | Substring match on linked gallery category. |
| `provider` | `string` | Substring match on linked gallery provider. |
| `uploader` | `string` | Substring match on linked gallery uploader. |
| `source_type` | `string` | Archive source type (e.g. `web`, `local`, `torrent`). Supports comma lists and `-` negation. |
| `reason` | `string` | Archive download reason. Supports comma lists and `-` negation. |
| `match_type` | `string` | Match algorithm type used to identify archive. |
| `rating_from` | `float` | Minimum gallery rating. |
| `rating_to` | `float` | Maximum gallery rating. |
| `filecount_from` | `integer` | Minimum filecount. |
| `filecount_to` | `integer` | Maximum filecount. |
| `filesize_from` | `integer` | Minimum size in bytes. |
| `filesize_to` | `integer` | Maximum size in bytes. |
| `posted_from` | `integer` | Minimum posted Unix timestamp. |
| `posted_to` | `integer` | Maximum posted Unix timestamp. |
| `created_from` | `integer` | Minimum creation date in DB *(authenticated only)*. |
| `created_to` | `integer` | Maximum creation date in DB *(authenticated only)*. |
| `extracted` | `boolean` | When set to `1`, only return archives that have been unpacked on disk. |
| `sort` | `string` | Sort field: `title`, `title_jpn`, `filesize`, `filecount`, `posted`, `public_date`, `create_date`, `reason`, `source_type`. |
| `asc_desc` | `string` | `desc` for descending order, `asc` for ascending. Default is descending. |
| `sort_by` | `string` (JSON) | Advanced multi-field sorting JSON: `[{"id": "public_date", "desc": true}]`. |
