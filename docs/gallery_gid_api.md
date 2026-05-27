# Gallery GID API Documentation

This document describes how to use the API to fetch `Gallery` entries by provider `gid` (gallery ID on the source site).

All endpoints use the public JSON API at `/api`. Authentication is optional: unauthenticated clients only receive galleries with `public=true`. Authenticated users (session or API token) can also access non-public galleries.

When `provider` is omitted, the first matching gallery for each `gid` is returned (same behavior as the legacy single-`gid` lookup). When `provider` is set, it is a single value applied to every lookup.

## Get Single Gallery by GID

**Endpoint**: `/api`  
**Method**: `GET`  
**Query Parameters**:

| Parameter | Required | Description |
| :--- | :--- | :--- |
| `gid` | Yes | Gallery ID on the provider (e.g. exhentai / panda numeric id). |
| `provider` | No | Provider slug (e.g. `panda`, `nhentai`). Restricts the match to that provider. |

Returns one gallery object.

### Example Request

```bash
curl "https://example.chaika.moe/api?gid=1234567"
```

With provider:

```bash
curl "https://example.chaika.moe/api?gid=1234567&provider=panda"
```

With authentication (to access non-public galleries):

```bash
curl "https://example.chaika.moe/api?gid=1234567&provider=panda" \
  -H "Authorization: Bearer YOUR_API_TOKEN"
```

### Response

**Success (200 OK)**

Returns a JSON object with the fields listed in [Response fields](#response-fields).

**Error (404 Not Found)**

Returned if no gallery matches the `gid` (and `provider`, if given), or the match is not public and the caller is not authenticated.

```json
{
  "result": "Gallery does not exist."
}
```

## Get Multiple Galleries by GID

**Endpoint**: `/api`  
**Method**: `GET`  
**Query Parameters**:

| Parameter | Required | Description |
| :--- | :--- | :--- |
| `gids` | Yes (repeatable) | One or more provider gallery IDs. Repeat the parameter for each id: `gids=...&gids=...`. |
| `provider` | No | Single provider slug applied to every `gids` value. |

Returns a JSON array of gallery objects. GIDs with no match, or a match that is not accessible to the caller, are omitted from the array (no error for individual misses).

### Example Request

```bash
curl "https://example.chaika.moe/api?gids=1234567&gids=7654321"
```

With a single provider for all GIDs:

```bash
curl "https://example.chaika.moe/api?gids=1234567&gids=7654321&provider=panda"
```

With authentication:

```bash
curl "https://example.chaika.moe/api?gids=1234567&gids=7654321&provider=panda" \
  -H "Authorization: Bearer YOUR_API_TOKEN"
```

### Response

**Success (200 OK)**

```json
[
  {
    "gid": "1234567",
    "title": "Example",
    "link": "https://...",
    "tags": ["language:english", "..."],
    "archives": []
  },
  {
    "gid": "7654321",
    "title": "Another",
    "link": "https://...",
    "tags": [],
    "archives": [{"id": 42, "download": "https://.../archive/42/download/"}]
  }
]
```

Returns `[]` if no `gids` parameters are provided.

The order of results follows the order of `gids` in the request; missing entries are skipped without placeholders.

## Response fields

Both `gid` and `gids` return objects with these fields:

| Field | Type | Description |
| :--- | :--- | :--- |
| `gid` | `string` | Provider gallery ID |
| `title` | `string` | Title |
| `title_jpn` | `string` | Japanese title |
| `category` | `string` | Category |
| `uploader` | `string` | Uploader |
| `posted` | `integer` | Posted date as Unix timestamp |
| `filecount` | `integer` | Page / file count |
| `filesize` | `integer` | Total size in bytes |
| `expunged` | `boolean` | Expunged flag |
| `disowned` | `boolean` | Disowned flag |
| `rating` | `number` | Rating |
| `fjord` | `boolean` | Fjord flag |
| `link` | `string` | Provider gallery URL |
| `tags` | `array[string]` | Tag list |
| `archives` | `array[object]` | Archives for this gallery visible to the caller |

Each archive entry contains:

| Field | Type | Description |
| :--- | :--- | :--- |
| `id` | `integer` | Archive primary key |
| `download` | `string` | Absolute URL to download the archive |

For unauthenticated requests, `archives` only includes public archives. Authenticated callers receive archives according to the same rules as other gallery API endpoints.
