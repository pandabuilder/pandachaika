import logging
import typing
from typing import Any

from core.base.auto_wanted import WantedParam, WantedRunStats, create_wanted_gallery
from core.base.utilities import format_title_to_wanted_search
from viewer.models import Artist, Gallery, WantedGallery
from . import constants, utilities

if typing.TYPE_CHECKING:
    from core.base.setup import Settings
    from viewer.models import AutoWantedSource
    from .parsers import Parser

logger = logging.getLogger(__name__)

# The old mugimugi provider's galleries are the same books, with the same gids.
KNOWN_PROVIDERS = (constants.provider_name, "mugimugi")

YES_NO = (("", "Any"), ("1", "Yes"), ("0", "No"))
ITEM_HELP = "Name or id. Separate alternatives with |."

# The JSON API's book search filters (GET /api/v1/books), plus stop_page.
WANTED_PARAMS = [
    WantedParam("q", "Title", help="Title text, searched in every title of the book."),
    WantedParam(
        "match",
        "Title match",
        kind="choice",
        choices=(
            ("", "Default (every word)"),
            ("words", "Every word"),
            ("contains", "Contains"),
            ("prefix", "Starts with"),
            ("suffix", "Ends with"),
            ("exact", "Exact"),
        ),
    ),
    WantedParam("circle", "Circle", help=ITEM_HELP),
    WantedParam("author", "Author", help=ITEM_HELP),
    WantedParam("parody", "Parody", help=ITEM_HELP),
    WantedParam("character", "Character", help=ITEM_HELP),
    WantedParam("contents", "Contents", help=ITEM_HELP),
    WantedParam("genre", "Genre", help=ITEM_HELP),
    WantedParam(
        "type",
        "Type",
        placeholder="Commercial Magazine",
        help="Book type name or id (Doujinshi 1, Manga 5, Commercial Magazine 7...). Separate alternatives with |.",
    ),
    WantedParam("convention", "Convention", help=ITEM_HELP),
    WantedParam("collections", "Collection", help=ITEM_HELP),
    WantedParam("publisher", "Publisher", help=ITEM_HELP),
    WantedParam("imprint", "Imprint", help=ITEM_HELP),
    WantedParam("with", "With items", help="Item keys the books must have: C1,K25 means both, C1|C2 either."),
    WantedParam("without", "Without items", help="Item keys the books must not have, comma separated."),
    WantedParam("language", "Language", placeholder="japanese", help="Name or id. Comma means either."),
    WantedParam("adult", "18+", kind="choice", choices=YES_NO),
    WantedParam("anthology", "Anthology", kind="choice", choices=YES_NO),
    WantedParam("copyshi", "Copybook", kind="choice", choices=YES_NO),
    WantedParam("magazine", "Magazine", kind="choice", choices=YES_NO),
    WantedParam("released_from", "Released from", kind="date"),
    WantedParam("released_to", "Released to", kind="date"),
    WantedParam("pages_min", "Pages from", kind="int"),
    WantedParam("pages_max", "Pages to", kind="int"),
    WantedParam(
        "sort",
        "Sort",
        kind="choice",
        choices=(
            ("", "Default (date added)"),
            ("id", "Date added"),
            ("released", "Release date"),
            ("changed", "Last changed"),
            ("title", "Title"),
            ("title_en", "English title"),
            ("title_jp", "Japanese title"),
            ("pages", "Pages"),
        ),
    ),
    WantedParam(
        "order", "Order", kind="choice", choices=(("", "Default (descending)"), ("desc", "Descending"), ("asc", "Ascending"))
    ),
    WantedParam(
        "stop_page",
        "Stop page",
        kind="int",
        help="Last page to read. Default: until a page whose books are all known (or the last one).",
    ),
]


def build_query(params: dict[str, Any]) -> dict[str, Any]:
    """The API query of a source's parameters (without the page)."""
    query: dict[str, Any] = {"sort": "id", "order": "desc", "per_page": constants.page_size, "count": 0}
    query.update({name: value for name, value in params.items() if name != "stop_page"})
    return query


def wanted_generator(settings: "Settings", source: "AutoWantedSource", stats: WantedRunStats) -> None:
    parser = typing.cast(
        "Parser", settings.provider_context.get_parsers(settings, filter_name=constants.provider_name)[0]
    )
    base = parser.base_url

    if not base:
        stats.error("{}: providers.{}.general.url is not set.".format(constants.provider_name, constants.provider_name))
        return

    query_values = build_query(source.params)
    stop_page = source.params.get("stop_page")
    force_process = source.force_process
    page = 1

    while True:
        logger.info(
            "Querying {} for auto wanted galleries, page: {}, source: {}, query: {}".format(
                constants.provider_name, page, source.name, query_values
            )
        )

        data = parser.api_get(base + constants.books_path, {**query_values, "page": page})

        if data is None:
            stats.error(
                "For provider {}: Got to page {}, but did not get a response, stopping".format(
                    constants.provider_name, page
                )
            )
            break

        results = data.get("results") or []
        api_galleries = [x for x in (utilities.book_to_gallery_data(book, base) for book in results) if x]

        if not api_galleries:
            logger.info("For provider {}: page {} has no books, stopping".format(constants.provider_name, page))
            break

        used_gids = set(
            Gallery.objects.filter(gid__in=[x.gid for x in api_galleries], provider__in=KNOWN_PROVIDERS).values_list(
                "gid", flat=True
            )
        )

        logger.info(
            "For provider {}: Page has {} galleries, from which {} are already present in the database.".format(
                constants.provider_name, len(api_galleries), len(used_gids)
            )
        )

        # If every gallery of the page is in the database, we assume we already processed everything.
        # The source's force_process processes everything anyway.
        if not force_process and len(used_gids) == len(api_galleries):
            logger.info(
                "For provider {}: Got to page {}, it has already been processed entirely, stopping".format(
                    constants.provider_name, page
                )
            )
            break

        for gallery_data in api_galleries:
            if gallery_data.gid in used_gids:
                continue
            stats.links += 1
            if not gallery_data.dl_type:
                gallery_data.dl_type = "auto_wanted"
            if source.reason:
                gallery_data.reason = source.reason
            gallery = Gallery.objects.add_from_values(gallery_data)

            if not gallery.title_jpn:
                continue

            publisher_name = ""
            publisher = gallery.tags.filter(scope="publisher").first()
            if publisher:
                publisher_name = publisher.name

            search_title = format_title_to_wanted_search(gallery.title_jpn)

            wanted_galleries: typing.Iterable[WantedGallery] = WantedGallery.objects.filter(
                title_jpn=gallery.title_jpn, search_title=search_title
            )

            if not wanted_galleries:
                wanted_gallery = create_wanted_gallery(
                    source,
                    stats,
                    title=gallery.title or gallery.title_jpn,
                    title_jpn=gallery.title_jpn,
                    search_title=search_title,
                    book_type=gallery.category,
                    page_count=gallery.filecount,
                    publisher=publisher_name,
                    category="Manga",
                )

                for artist in gallery.tags.filter(scope="artist"):
                    artist_obj = Artist.objects.filter(name_jpn=artist.name).first()
                    if not artist_obj:
                        artist_obj = Artist.objects.create(name=artist.name, name_jpn=artist.name)
                    wanted_gallery.artists.add(artist_obj)
                logger.info(
                    "Created wanted gallery ({}): {}, search title: {}".format(
                        wanted_gallery.book_type, wanted_gallery.get_absolute_url(), gallery.title_jpn
                    )
                )

                wanted_galleries = [wanted_gallery]

            for wanted_gallery in wanted_galleries:
                mention, mention_created = wanted_gallery.mentions.get_or_create(
                    mention_date=gallery.create_date,
                    release_date=gallery.posted,
                    type="release_date",
                    source=constants.provider_name,
                )
                if mention_created:
                    stats.mentions += 1
                if mention_created and gallery.thumbnail:
                    mention.copy_img(gallery.thumbnail.path)
                    wanted_gallery.calculate_nearest_release_date()

        if len(results) < query_values["per_page"]:
            logger.info("Got to page {}, the last one, stopping".format(page))
            break

        if stop_page and page >= stop_page:
            logger.info("Got to page {}, the source's stop page, stopping".format(page))
            break

        page += 1

    logger.info("{} Auto wanted ended for source: {}.".format(constants.provider_name, source.name))
