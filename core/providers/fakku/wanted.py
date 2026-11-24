import logging
import typing
import urllib.parse

from bs4 import BeautifulSoup

from core.base.auto_wanted import WantedParam, WantedRunStats, create_wanted_gallery
from core.base.utilities import provider_web_request, format_title_to_wanted_search, construct_request_dict
from viewer.models import Gallery, WantedGallery, Provider, Artist
from . import constants

if typing.TYPE_CHECKING:
    from core.base.setup import Settings
    from viewer.models import AutoWantedSource

logger = logging.getLogger(__name__)


WANTED_PARAMS = [
    WantedParam(
        "subpath", "Subpath", required=True, placeholder="books", help="Listing to read, e.g. books or tags/doujin."
    ),
    WantedParam(
        "container_tag",
        "Container selector",
        required=True,
        placeholder="div",
        help="CSS selector of the container of each gallery link.",
    ),
    WantedParam("link_tag", "Link selector", placeholder="a", help="CSS selector of the link inside the container."),
    WantedParam(
        "url_attribute_name", "URL attribute", placeholder="href", help="Attribute of the link that has the URL."
    ),
    WantedParam(
        "link_attribute_get_text",
        "URL is the container text",
        kind="bool",
        help="Read the URL from the container's text instead of a link inside it.",
    ),
]


def wanted_generator(settings: "Settings", source: "AutoWantedSource", stats: WantedRunStats) -> None:
    own_settings = settings.providers[constants.provider_name]

    query_values: dict = {"page": 1, **source.params}

    provider, provider_created = Provider.objects.get_or_create(
        slug=constants.provider_name, defaults={"name": constants.provider_name}
    )

    parser = settings.provider_context.get_parsers(settings, filter_name=constants.provider_name)[0]
    force_process = source.force_process

    while True:
        logger.info(
            "Querying {} for auto wanted galleries, source: {}, options: {}".format(
                constants.provider_name, source.name, str(query_values)
            )
        )

        if "subpath" not in query_values:
            stats.error("Cannot query without setting a subpath for {}".format(source.name))
            break
        subpath = query_values["subpath"]

        if not {"container_tag"}.issubset(query_values.keys()):
            stats.error("Cannot query without html container definition for {}".format(source.name))
            break
        container_tag = query_values["container_tag"]

        get_text_from_container = False
        link_tag = ""
        url_attribute_name = ""

        if "link_attribute_get_text" in query_values and query_values["link_attribute_get_text"]:
            get_text_from_container = True
        else:
            if not {"link_tag", "url_attribute_name"}.issubset(query_values.keys()):
                stats.error("Cannot query without link container definition for {}".format(source.name))
                break
            link_tag = query_values["link_tag"]
            url_attribute_name = query_values["url_attribute_name"]

        full_url = urllib.parse.urljoin("{}/".format(subpath), "page/{}".format(query_values["page"]))

        link = urllib.parse.urljoin(constants.main_url, full_url)

        request_dict = construct_request_dict(settings, own_settings)

        response = provider_web_request(constants.provider_name, own_settings.wait_timer)(
            link,
            request_dict,
            post=False,
        )

        if not response:
            stats.error(
                "For provider {}: Got to page {}, but did not get a response, stopping".format(
                    constants.provider_name, query_values["page"]
                )
            )
            break

        response.encoding = "utf-8"

        soup = BeautifulSoup(response.text, "html.parser")

        gallery_containers = soup.select(container_tag)

        n_containers = len(gallery_containers)

        gallery_links: list[str] = []
        gallery_gids: list[str] = []

        for gallery_container in gallery_containers:
            if get_text_from_container:
                gallery_link = gallery_container.get_text()
            else:
                gallery_url_containers = gallery_container.select(link_tag)

                if len(gallery_url_containers) > 0:
                    gallery_url_container = gallery_url_containers[0]
                    if gallery_url_container.has_attr(url_attribute_name):
                        url_possible_link = gallery_url_container[url_attribute_name]
                        if isinstance(url_possible_link, str):
                            gallery_link = url_possible_link
                        else:
                            continue
                    else:
                        continue
                else:
                    continue

            gallery_gids.append(gallery_link[1:])

        if not gallery_gids:
            stats.error(
                "For provider {}: Got to url: {}, but could not parse the response into galleries, stopping. Number of gallery containers found: {}.".format(
                    constants.provider_name, full_url, n_containers
                )
            )
            break

        # Listen to what the server says

        used = Gallery.objects.filter(gid__in=gallery_gids, provider=constants.provider_name)

        # If the amount of galleries present in database is equal to what we get from the page,
        # we assume we already processed everything. The source's force_process processes everything anyway.

        logger.info(
            "Page has {} galleries, from which {} are already present in the database.".format(
                len(gallery_gids), used.count()
            )
        )

        if not force_process and used.count() == len(gallery_gids):
            logger.info("Got to page {}, it has already been processed entirely, stopping".format(query_values["page"]))
            break

        used_gids = used.values_list("gid", flat=True)

        for gallery_gid in gallery_gids:
            if gallery_gid not in used_gids:
                gallery_link = urllib.parse.urljoin(constants.main_url, "/" + gallery_gid)

                gallery_links.append(gallery_link)

        api_galleries = parser.fetch_multiple_gallery_data(gallery_links)

        if not api_galleries:
            stats.error(
                "Got to page {}, but could not parse the gallery link into GalleryData instances".format(
                    query_values["page"]
                )
            )
            break

        for gallery_data in api_galleries:
            if gallery_data.gid not in used_gids:
                stats.links += 1
                if not gallery_data.dl_type:
                    gallery_data.dl_type = "auto_wanted"
                gallery_data.reason = source.reason or "backup"
                gallery = Gallery.objects.add_from_values(gallery_data)
                # We match anyway in case there's a previous WantedGallery.
                # Actually, we don't match since we only get metadata here, so it should not count as found.
                publisher_name = ""
                publisher = gallery.tags.filter(scope="publisher").first()
                if publisher:
                    publisher_name = publisher.name

                if not gallery.title:
                    continue

                search_title = format_title_to_wanted_search(gallery.title)

                wanted_galleries: typing.Iterable[WantedGallery] = WantedGallery.objects.filter(
                    title=gallery.title, search_title=search_title
                )

                if not wanted_galleries:
                    wanted_gallery = create_wanted_gallery(
                        source,
                        stats,
                        title=gallery.title,
                        title_jpn=gallery.title_jpn,
                        search_title=search_title,
                        book_type=gallery.category,
                        page_count=gallery.filecount,
                        publisher=publisher_name,
                    )

                    for artist in gallery.tags.filter(scope="artist"):
                        artist_obj = Artist.objects.filter(name=artist.name).first()
                        if not artist_obj:
                            artist_obj = Artist.objects.create(name=artist.name)
                        wanted_gallery.artists.add(artist_obj)
                    logger.info(
                        "Created wanted gallery ({}): {}, search title: {}".format(
                            wanted_gallery.book_type, wanted_gallery.get_absolute_url(), wanted_gallery.search_title
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

        # galleries.extend(api_galleries)

        # API returns 25 max results per query, so if we get 24 or less, means there's no more pages.
        # API Manual says 25, but we get 50 results normally!
        if len(api_galleries) < 1:
            logger.info(
                "Got to page {}, and we got less than 1 gallery, meaning there is no more pages, stopping".format(
                    query_values["page"]
                )
            )
            break

        query_values["page"] += 1

    logger.info("{} Auto wanted ended for source: {}.".format(constants.provider_name, source.name))
