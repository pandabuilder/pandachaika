import logging
import re
import typing
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from core.base.types import GalleryData
from core.base.utilities import translate_tag
from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import Gallery

logger = logging.getLogger(__name__)

# Book ids in the accepted links: doujinshi.org/book/123/, <mirror>/#/book/123, <mirror>/api/v1/books/B123.
BOOK_ID_REGEX = re.compile(r"/books?/B?(\d+)(?![\w.])")
# Books of an item on the mirror's frontend: <mirror>/#/item/circle/8594.
ITEM_PAGE_REGEX = re.compile(r"#/item/(\w+)/(\d+)")
# Books of an item on the API: <mirror>/api/v1/items/circle/8594/books?...
ITEM_BOOKS_API_REGEX = re.compile(r"api/v1/items/(\w+)/(\d+)/books")

# The mirror's image URLs (<url>images/<size>/<bucket>/<id>.jpg) and the original image server's.
MIRROR_IMAGE_PATH_REGEX = re.compile(r"images/(big|tn)/(\d+)/(\d+)\.jpg$")
ORIGINAL_IMAGE_URL_REGEX = re.compile(r"^https?://img\.doujinshi\.org/(big|tn)/(\d+)/(\d+)\.jpg$")

# Bracketed parts of file names: (event), [circle (author)], (parody), [language], {group}, 【...】.
BRACKETED_REGEX = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\}|【[^【】]*】")

# How the item types become tag scopes. "type" is the category, not a tag.
TAG_SCOPES = {
    "circle": "group",
    "author": "artist",
    "parody": "parody",
    "character": "character",
    "convention": "convention",
    "collections": "collections",
    "contents": "contents",
    "genre": "genre",
    "publisher": "publisher",
    "imprint": "imprint",
}
# Placeholder items that many books link to (No Convention: 214K books, contents Unknown: 1.59M), which say nothing
# about the book. By item type and lowercase English name.
PLACEHOLDER_ITEMS = {
    ("convention", "no convention"),
    ("collections", "no collections"),
    ("parody", "no parody"),
    ("publisher", "no publisher"),
    ("publisher", "not set"),
    ("imprint", "no imprint"),
    ("contents", "unknown"),
}


def base_url(url: str) -> str:
    """The mirror's base URL with a trailing slash, or empty when not set."""
    url = (url or "").strip()
    if not url:
        return ""
    return url if url.endswith("/") else url + "/"


def configured_base_url() -> str:
    """The base URL from the running settings, empty when the provider isn't configured."""
    from django.conf import settings

    crawler_settings = getattr(settings, "CRAWLER_SETTINGS", None)
    providers = getattr(crawler_settings, "providers", None) or {}
    own_settings = providers.get(constants.provider_name) if hasattr(providers, "get") else None
    return base_url(getattr(own_settings, "url", "") or "")


def gid_from_book_id(book_id: int | str) -> str:
    return "{}{}".format(constants.gid_prefix, book_id)


def book_id_from_gid(gid: str) -> int | None:
    if gid and gid.startswith(constants.gid_prefix) and gid[len(constants.gid_prefix):].isdigit():
        return int(gid[len(constants.gid_prefix):])
    return None


def book_page_url(base: str, book_id: int | str) -> str:
    """The book's page on the mirror, or on the original site when there's no mirror set."""
    if base:
        return "{}{}{}".format(base_url(base), constants.book_page_path, book_id)
    return "{}/book/{}/".format(constants.original_page, book_id)


def resolve_url(gallery: "Gallery") -> str:
    book_id = book_id_from_gid(gallery.gid)
    if book_id is None:
        return "Can't reconstruct URL"
    return book_page_url(configured_base_url(), book_id)


def stored_image_url(url: str, base: str) -> str:
    """The image URL to store: an image on the mirror as its original image server URL, others (an external image
    host set on the server) as they are."""
    if url and is_mirror_url(url, base):
        m = MIRROR_IMAGE_PATH_REGEX.search(url.split("?", 1)[0])
        if m:
            return constants.original_image_url.format(size=m.group(1), bucket=m.group(2), id=m.group(3))
    return url


def resolve_thumbnail_url(gallery: "Gallery") -> str:
    """Where to fetch a stored original image server URL from: the same image on the current mirror.

    Without a mirror set, the stored URL itself (the original server is gone, so it fails like any dead link)."""
    url = gallery.thumbnail_url or ""
    m = ORIGINAL_IMAGE_URL_REGEX.match(url)
    base = configured_base_url()
    if not m or not base:
        return url
    return "{}{}{}/{}/{}.jpg".format(base, constants.images_path, m.group(1), m.group(2), m.group(3))


def _without_scheme(url: str) -> str:
    return re.sub(r"^\w+://", "", url).lower()


def is_mirror_url(url: str, base: str) -> bool:
    return bool(base) and _without_scheme(url).startswith(_without_scheme(base_url(base)))


def is_original_book_url(url: str) -> bool:
    return any(part in url for part in constants.original_book_urls)


def book_id_from_url(url: str) -> int | None:
    m = BOOK_ID_REGEX.search(url)
    if m:
        return int(m.group(1))
    return None


# Search syntax of other sites in a title query (the release series backlog query: "{title}" -language:translated$):
# negated terms, and tag terms with a quoted value or anchored with $. A plain "a:b" stays, titles have those (Re:Zero).
FOREIGN_QUERY_TERMS_REGEX = re.compile(r'(?<!\S)-\S+|[\w.]+:"[^"]*"\$?|[\w.]+:\S*\$(?!\S)')
TITLE_QUERY_PARAMS = ("q", "title", "sn")


def clean_title_query(text: str) -> str:
    """A title query without other sites' search syntax, which the mirror would read as title words."""
    text = FOREIGN_QUERY_TERMS_REGEX.sub(" ", text)
    return " ".join(text.replace('"', " ").replace("$", " ").split())


def search_api_request(url: str, base: str) -> tuple[str, dict[str, str]] | None:
    """The API request (URL, query parameters) of a search page on the mirror, None if it's not one.

    Search pages are the frontend's book lists (``#/books?...``, ``#/item/<type>/<id>``) and the API's book searches
    (``api/v1/books?...``, ``api/v1/items/<type>/<id>/books?...``). Their filters are the API's, so they pass as is,
    except the title query, cleaned with :func:`clean_title_query`.
    """
    request = _search_api_request(url, base)
    if request is None:
        return None
    api_url, params = request
    for name in TITLE_QUERY_PARAMS:
        if name in params:
            params[name] = clean_title_query(params[name])
    return api_url, params


def _search_api_request(url: str, base: str) -> tuple[str, dict[str, str]] | None:
    if not is_mirror_url(url, base):
        return None
    base = base_url(base)
    parts = urlsplit(url)
    fragment = parts.fragment
    if fragment:
        path, _, query = fragment.partition("?")
        params = dict(parse_qsl(query))
        if path.rstrip("/") == "/books":
            return base + constants.books_path, params
        m = ITEM_PAGE_REGEX.search(url)
        if m:
            return "{}{}items/{}/{}/books".format(base, constants.api_path, m.group(1), m.group(2)), params
        return None
    params = dict(parse_qsl(parts.query))
    path = parts.path.rstrip("/")
    if path.endswith("/" + constants.books_path) or path.endswith("/" + constants.books_path + "/search"):
        return base + constants.books_path, params
    m = ITEM_BOOKS_API_REGEX.search(path)
    if m:
        return "{}{}items/{}/{}/books".format(base, constants.api_path, m.group(1), m.group(2)), params
    return None


def title_part(text: str) -> str:
    """The title in a name like ``(C92) [PLANT (Tsurui)] Hibiki to Pool! (THE IDOLM@STER) [English]``.

    Removes the bracketed parts (nested ones from the inside out). Falls back to the text when nothing is left.
    """
    title = text
    while True:
        stripped = BRACKETED_REGEX.sub(" ", title)
        if stripped == title:
            break
        title = stripped
    title = " ".join(title.replace("_", " ").split())
    return title or text.strip()


def item_name(item: dict[str, Any]) -> str:
    return (item.get("name_en") or item.get("name_jp") or "").strip()


def book_to_gallery_data(book: dict[str, Any], base: str) -> GalleryData | None:
    """A book of the JSON API (``/api/v1/books``) as GalleryData. The tags are the old mugimugi provider's."""
    book_id = book.get("id")
    if not isinstance(book_id, int):
        return None

    gallery = GalleryData(gid_from_book_id(book_id), constants.provider_name)
    gallery.link = book_page_url(base, book_id)
    gallery.title = book.get("name_en") or ""
    gallery.title_jpn = book.get("name_jp") or ""
    gallery.comment = book.get("info") or ""
    gallery.category = book.get("type") or ""
    gallery.filesize = 0
    gallery.filecount = book.get("pages") or 0
    gallery.uploader = ""
    # The cover (big) if the mirror has it, else the small thumbnail (tn, 110x155). The mirror returns null for a
    # size it serves from indexed archives that don't have the book's image.
    images = book.get("images") or {}
    gallery.thumbnail_url = stored_image_url(images.get("big") or images.get("thumb") or "", base)

    released = book.get("released")
    if released:
        try:
            gallery.posted = datetime.strptime(released, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning("Book {}: could not parse release date: {}".format(book_id, released))

    tags: list[str] = []
    for item_type, items in (book.get("links") or {}).items():
        if item_type == "type":
            # The category, already set from the book's own type.
            if not gallery.category and items:
                gallery.category = item_name(items[0])
            continue
        scope = TAG_SCOPES.get(item_type, item_type)
        for item in items or []:
            name = item_name(item)
            if not name or (item_type, name.lower()) in PLACEHOLDER_ITEMS:
                continue
            tags.append(translate_tag("{}:{}".format(scope, name)))

    language = (book.get("language") or {}).get("name")
    if language and language.lower() != "unknown":
        tags.append(translate_tag("language:{}".format(language)))

    if book.get("adult") is False:
        tags.append(translate_tag("non-h"))

    gallery.tags = list(dict.fromkeys(tags))

    return gallery
