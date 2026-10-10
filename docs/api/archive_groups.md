# Archive Group Endpoints API Documentation

This document describes all API endpoints for managing `ArchiveGroup` and `ArchiveGroupEntry` records (series, anthologies, multi-volume collections) in Panda Backup.

All endpoints use the public JSON API at `/api` (or `/jsearch`).

---

## Authentication & Permissions

- **Read Operations (`GET`)**: Unauthenticated callers can view archive groups and entries where `public=true`. Authenticated callers can view both public and non-public groups.
- **Write Operations (`POST`, `PUT`, `DELETE`)**: Require an authenticated user with explicit model permissions:
  - `viewer.change_archivegroup`: Required to create, update, or delete an `ArchiveGroup`.
  - `viewer.change_archivegroupentry`: Required to create or update an `ArchiveGroupEntry`.
  - `viewer.delete_archivegroup`: Required to delete an `ArchiveGroup`.
  - `viewer.delete_archivegroupentry`: Required to delete an `ArchiveGroupEntry`.

Requests lacking authentication or required permissions return `403 Forbidden` (`{"result": "Not authorized"}`).

---

## Endpoint Overview

| Command Parameter | Method | Permission | Description |
| :--- | :--- | :--- | :--- |
| `?archive-group=<id>` | `GET` | None | Get an archive group with all its ordered entries |
| `?archive-group-entry=<id>` | `GET` | None | Get a single archive group entry |
| `?archive-group-entry-archive=<id>` | `GET` | None | Get archive formatted for an entry |
| `?archive-group=` | `POST` | `viewer.change_archivegroup` | Create an archive group with initial entries |
| `?archive-group-entry=<group_id>` | `POST` | `viewer.change_archivegroupentry` | Add an entry to an archive group |
| `?archive-group=<group_id>` | `PUT` | `viewer.change_archivegroup` | Update an archive group and its entries |
| `?archive-group-entry=<entry_id>` | `PUT` | `viewer.change_archivegroupentry` | Update an individual archive group entry |
| `?archive-group=<group_id>` | `DELETE` | `viewer.delete_archivegroup` | Delete an archive group |
| `?archive-group-entry=<entry_id>` | `DELETE` | `viewer.delete_archivegroupentry` | Delete an archive group entry |

---

## 1. Get Archive Group (`GET ?archive-group=<id>`)

Fetches an ArchiveGroup and all entries belonging to it, ordered by position.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?archive-group=<id>`

### Example Request
```bash
curl "https://example.chaika.moe/api?archive-group=1"
```

### Response
**Success (200 OK)**
```json
{
  "id": 1,
  "title": "Example Series",
  "title_slug": "example-series",
  "details": "Full series description.",
  "position": 1,
  "public": true,
  "create_date": 1600000000,
  "last_modified": 1600000000,
  "archive_group_entries": [
    {
      "id": 10,
      "title": "Volume 1",
      "position": 1,
      "archive": {
        "id": 42,
        "title": "Example Series Vol. 1",
        "title_jpn": "Example Series 1 JPN",
        "filecount": 180,
        "filesize": 95000000,
        "posted": 1546300800,
        "public_date": 1550000000,
        "create_date": 1549000000,
        "source": "web",
        "reason": "Series import",
        "category": "Manga",
        "uploader": "UploaderName",
        "rating": "4.8",
        "link": "https://exhentai.org/g/1234567/abcdef/",
        "download": "https://example.chaika.moe/archive/42/download/",
        "url": "https://example.chaika.moe/archive/42/",
        "thumbnail": "https://example.chaika.moe/media/thumb/42.jpg",
        "tags": ["artist:sample"]
      }
    }
  ]
}
```

**Error (404 Not Found)**
```json
{
  "result": "ArchiveGroup does not exist."
}
```

---

## 2. Get Archive Group Entry (`GET ?archive-group-entry=<id>`)

Fetches a single ArchiveGroupEntry by its ID.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?archive-group-entry=<id>`

### Example Request
```bash
curl "https://example.chaika.moe/api?archive-group-entry=10"
```

### Response
Returns the single entry object with its embedded `archive` (same schema as above).

---

## 3. Get Archive Formatted for Entry (`GET ?archive-group-entry-archive=<id>`)

Fetches archive details formatted specifically for archive group entry representations.

- **Endpoint**: `/api`
- **Method**: `GET`
- **Query Parameters**: `?archive-group-entry-archive=<archive_id>`

### Example Request
```bash
curl "https://example.chaika.moe/api?archive-group-entry-archive=42"
```

### Response
Returns the archive object matching the embedded entry `archive` schema.

---

## 4. Create Archive Group (`POST ?archive-group=`)

Creates a new ArchiveGroup with optional initial entries.

- **Endpoint**: `/api`
- **Method**: `POST`
- **Query Parameter**: `?archive-group=`
- **Permissions**: `viewer.change_archivegroup`
- **Headers**:
  - `Content-Type: application/json`
  - `Authorization: Bearer YOUR_API_TOKEN`

### Request Body Schema
```json
{
  "title": "Series Name",
  "title_slug": "series-name",
  "details": "Description of the collection",
  "position": 1,
  "archive_group_entries": [
    {
      "title": "Vol. 1",
      "position": 1,
      "archive": {
        "id": 42
      }
    },
    {
      "title": "Vol. 2",
      "position": 2,
      "archive": {
        "id": 43
      }
    }
  ]
}
```

### Response
Returns the complete created ArchiveGroup object (`200 OK`) with its generated `id` and all saved entries.

---

## 5. Add Entry to Archive Group (`POST ?archive-group-entry=<group_id>`)

Appends a new entry into an existing ArchiveGroup.

- **Endpoint**: `/api`
- **Method**: `POST`
- **Query Parameter**: `?archive-group-entry=<group_id>`
- **Permissions**: `viewer.change_archivegroupentry`

### Request Body Schema
```json
{
  "title": "Vol. 3",
  "position": 3,
  "archive": {
    "id": 44
  }
}
```

### Response
**Success (200 OK)**  
Returns the created entry object with full archive details:
```json
{
  "id": 12,
  "title": "Vol. 3",
  "position": 3,
  "archive": {
    "id": 44,
    "title": "Series Vol 3",
    "download": "https://example.chaika.moe/archive/44/download/"
  }
}
```

---

## 6. Update Archive Group (`PUT ?archive-group=<group_id>`)

Updates the metadata of an ArchiveGroup and modifies/reorders its entries atomically.

- **Endpoint**: `/api`
- **Method**: `PUT`
- **Query Parameter**: `?archive-group=<group_id>`
- **Permissions**: `viewer.change_archivegroup`

### Request Body Schema
```json
{
  "title": "Updated Series Title",
  "details": "Updated series notes",
  "position": 2,
  "archive_group_entries": [
    {
      "id": 10,
      "title": "Vol. 1 (Revised)",
      "position": 1,
      "archive": {
        "id": 42
      }
    },
    {
      "id": 11,
      "title": "Vol. 2 (Revised)",
      "position": 2,
      "archive": {
        "id": 43
      }
    }
  ]
}
```

### Response
Returns the updated ArchiveGroup object (`200 OK`).

---

## 7. Update Archive Group Entry (`PUT ?archive-group-entry=<entry_id>`)

Updates an individual entry's title, position, or linked archive.

- **Endpoint**: `/api`
- **Method**: `PUT`
- **Query Parameter**: `?archive-group-entry=<entry_id>`
- **Permissions**: `viewer.change_archivegroupentry`

### Request Body Schema
```json
{
  "title": "Special Episode",
  "position": 5,
  "archive": {
    "id": 55
  }
}
```

### Response
Returns the updated entry object (`200 OK`).

---

## 8. Delete Archive Group (`DELETE ?archive-group=<group_id>`)

Deletes an entire ArchiveGroup and its entries. Associated `Archive` files are preserved and not deleted.

- **Endpoint**: `/api`
- **Method**: `DELETE`
- **Query Parameter**: `?archive-group=<group_id>`
- **Permissions**: `viewer.delete_archivegroup`

### Example Request
```bash
curl -X DELETE "https://example.chaika.moe/api?archive-group=1" \
  -H "Authorization: Bearer YOUR_API_TOKEN"
```

### Response
```json
{
  "result": 1
}
```

---

## 9. Delete Archive Group Entry (`DELETE ?archive-group-entry=<entry_id>`)

Removes a single entry from an ArchiveGroup.

- **Endpoint**: `/api`
- **Method**: `DELETE`
- **Query Parameter**: `?archive-group-entry=<entry_id>`
- **Permissions**: `viewer.delete_archivegroupentry`

### Example Request
```bash
curl -X DELETE "https://example.chaika.moe/api?archive-group-entry=10" \
  -H "Authorization: Bearer YOUR_API_TOKEN"
```

### Response
```json
{
  "result": 1
}
```
