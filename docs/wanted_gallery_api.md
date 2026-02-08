# Wanted Gallery API Documentation

This document describes how to use the API to create `WantedGallery` entries.

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
