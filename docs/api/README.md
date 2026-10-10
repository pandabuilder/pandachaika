# Panda Backup Public JSON API

This documentation describes the public-facing, third-party JSON API for Panda Backup.

The API is served by the view `json_api` in [`viewer/views/api/routes.py`](file:///c:/Users/Netto/projects/pandagallery/viewer/views/api/routes.py) and routed at:
- `/api`
- `/jsearch` (alias)

This interface is stable, public-facing, and designed for consumption by third-party tools, scripts, browser extensions, and remote Panda instances.

---

## Architecture & Request Dispatch

Rather than relying on separate URL paths for every resource, the API uses a **query-parameter dispatch architecture** over standard HTTP methods (`GET`, `POST`, `PUT`, `DELETE`).

Each request is directed to `/api`, and the **first matching command parameter** in the query string dictates which handler processes the request (defined in [`viewer/views/api/registry.py`](file:///c:/Users/Netto/projects/pandagallery/viewer/views/api/registry.py) and [`viewer/views/api/dispatch.py`](file:///c:/Users/Netto/projects/pandagallery/viewer/views/api/dispatch.py)).

For example:
- `GET /api?gid=12345` &rarr; dispatches to the single gallery GID handler.
- `GET /api?archive=42` &rarr; dispatches to the single archive handler.
- `POST /api?wanted-gallery=` &rarr; dispatches to the create wanted gallery handler.
- `DELETE /api?archive-group=5` &rarr; dispatches to the delete archive group handler.

If no recognized command parameter is present in the query string:
- `GET`, `POST`, `PUT` return `200 OK` with `{"result": "Unknown command"}`.
- `DELETE` returns `404 Not Found` with `{"result": "Unknown command"}`.
- Unhandled HTTP methods (such as `PATCH` or `HEAD`) return `405 Method Not Allowed` with `{"result": "Unsupported request method"}`.

---

## Authentication & Authorization

Authentication is optional for read requests, but mandatory for non-public records and write operations:

### 1. Unauthenticated Requests
- Can read any entity marked as **public** (`public=true`).
- Requests for non-public entities return `404 Not Found` (e.g. `{"result": "Archive does not exist."}`).
- In search/list endpoints, non-public items are excluded from the result set.

### 2. Authenticated Requests
Authenticated callers have access to both public and private entities, internal creation dates, and administrative/write features.

Authentication is accepted via:
- **API Token (Long-Lived Bearer Token)**: Passed via the `Authorization` HTTP header:
  ```http
  Authorization: Bearer YOUR_API_TOKEN
  ```
  Tokens are verified against user tokens in the database with expiry checking.
- **Session Cookie**: Standard Django session authentication (e.g. active web session or via API login).

### 3. Login and Logout Endpoints
For session-based integration, helper endpoints are available in [`viewer/views/api/routes.py`](file:///c:/Users/Netto/projects/pandagallery/viewer/views/api/routes.py):

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
- **Logout**: `POST /api-logout/` (or `GET`)  
  Clears the session. Response:
  ```json
  {"success": true, "message": "Logged out."}
  ```

### 4. Permissions for Write Operations
All `POST`, `PUT`, and `DELETE` requests require an authenticated user with specific model permissions:

| Operation | Command Parameter | Required Django Permission |
| :--- | :--- | :--- |
| Create Archive Group | `POST ?archive-group=` | `viewer.change_archivegroup` |
| Add Archive Group Entry | `POST ?archive-group-entry=<group_id>` | `viewer.change_archivegroupentry` |
| Create Wanted Gallery | `POST ?wanted-gallery=` | `viewer.add_wantedgallery` |
| Update Archive Group | `PUT ?archive-group=<group_id>` | `viewer.change_archivegroup` |
| Update Archive Group Entry | `PUT ?archive-group-entry=<entry_id>` | `viewer.change_archivegroupentry` |
| Delete Archive Group | `DELETE ?archive-group=<group_id>` | `viewer.delete_archivegroup` |
| Delete Archive Group Entry | `DELETE ?archive-group-entry=<entry_id>` | `viewer.delete_archivegroupentry` |

Requests without required permissions return `403 Forbidden`:
```json
{
  "result": "Not authorized"
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
| **[wanted_galleries.md](wanted_galleries.md)** | Wanted Galleries & Tracking | `wanted-gallery`, `wanted-galleries`, `wanted-galleries-search` |
| **[archive_groups.md](archive_groups.md)** | Archive Groups & Collections | `archive-group`, `archive-group-entry`, `archive-group-entry-archive` |

---

## Endpoint Quick Reference

Below is the complete list of available commands on `/api`:

### GET Commands
| Key | Target | Parameters | Description |
| :--- | :--- | :--- | :--- |
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

### POST Commands
| Key | Target | Parameters | Permission | Description |
| :--- | :--- | :--- | :--- | :--- |
| `archive-group` | ArchiveGroup | `?archive-group=` | `viewer.change_archivegroup` | Create archive group with initial entries |
| `archive-group-entry` | ArchiveGroupEntry | `?archive-group-entry=<group_id>` | `viewer.change_archivegroupentry` | Append an entry to an archive group |
| `wanted-gallery` | WantedGallery | `?wanted-gallery=` | `viewer.add_wantedgallery` | Create a new wanted gallery |

### PUT Commands
| Key | Target | Parameters | Permission | Description |
| :--- | :--- | :--- | :--- | :--- |
| `archive-group` | ArchiveGroup | `?archive-group=<group_id>` | `viewer.change_archivegroup` | Update archive group metadata and entries |
| `archive-group-entry` | ArchiveGroupEntry | `?archive-group-entry=<entry_id>` | `viewer.change_archivegroupentry` | Update an existing archive group entry |

### DELETE Commands
| Key | Target | Parameters | Permission | Description |
| :--- | :--- | :--- | :--- | :--- |
| `archive-group` | ArchiveGroup | `?archive-group=<group_id>` | `viewer.delete_archivegroup` | Delete an archive group |
| `archive-group-entry` | ArchiveGroupEntry | `?archive-group-entry=<entry_id>` | `viewer.delete_archivegroupentry` | Delete an archive group entry |
