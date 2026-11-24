import logging
import typing
import urllib.parse

import feedparser
from bs4 import BeautifulSoup

from core.base.auto_wanted import WantedParam, WantedRunStats, create_wanted_gallery
from core.base.utilities import provider_web_request, format_title_to_wanted_search, construct_request_dict
from viewer.models import WantedGallery, Provider, Artist, ProcessedLinks
from . import constants, utilities
from .utilities import (
    get_on_sale_date_from_soup,
    PRODUCT_ID_MATCHER,
    parse_product_page,
    get_artists_from_soup,
    get_cover_image_from_soup,
)

if typing.TYPE_CHECKING:
    from core.base.setup import Settings
    from viewer.models import AutoWantedSource

logger = logging.getLogger(__name__)


WANTED_PARAMS = [
    WantedParam(
        "subpath",
        "Subpath",
        required=True,
        placeholder="comics/feed/",
        help="RSS feed to read (comics/feed/ for R18 単行本, magazine/feed/ for R18 雑誌), or the products list page "
        "(comics_index/) with 'Products list page'.",
    ),
    WantedParam(
        "url_html_type",
        "Products list page",
        kind="bool",
        help="The subpath is a products list page (read page by page) instead of an RSS feed.",
    ),
    WantedParam(
        "stop_page",
        "Stop page",
        kind="int",
        placeholder="1",
        help="Products list page: the last page read. Default: 1.",
    ),
]


def wanted_generator(settings: "Settings", source: "AutoWantedSource", stats: WantedRunStats) -> None:
    own_settings = settings.providers[constants.provider_name]
    params = source.params

    provider, provider_created = Provider.objects.get_or_create(
        slug=constants.provider_name, defaults={"name": constants.provider_name}
    )

    logger.info(
        "Querying {} for auto wanted galleries, source: {}, options: {}".format(
            constants.provider_name, source.name, str(params)
        )
    )

    subpath = params.get("subpath")
    if not subpath:
        stats.error("Cannot query without setting a subpath for {}".format(source.name))
        return

    request_dict = construct_request_dict(settings, own_settings)

    if params.get("url_html_type"):
        if "cookies" not in request_dict:
            request_dict["cookies"] = {}
        request_dict["cookies"]["isOver18"] = "true"
        process_products_page(source, stats, own_settings, provider, request_dict, subpath)
    else:
        process_feed_page(source, stats, own_settings, provider, request_dict, subpath)

    logger.info("{} Auto wanted ended for source: {}.".format(constants.provider_name, source.name))


def process_feed_page(source, stats, own_settings, provider, request_dict, subpath):
    full_url = "{}".format(subpath)

    gallery_gids = []

    link = urllib.parse.urljoin(constants.main_url, full_url)

    response = provider_web_request(constants.provider_name, own_settings.wait_timer)(
        link,
        request_dict,
        post=False,
    )

    if not response:
        stats.error(
            "For provider {}: URL: {}, did not give a response, stopping".format(constants.provider_name, link)
        )
        return

    response.encoding = "utf-8"

    feed = feedparser.parse(response.text)

    feed_galleries_data = [utilities.map_feed_data_to_internal(x) for x in feed["items"]]

    for feed_gallery_data in feed_galleries_data:

        gallery_link = feed_gallery_data["link"]

        if feed_gallery_data["uid"]:
            gallery_gids.append(feed_gallery_data["uid"])
        else:
            stats.error("Skipping link: {}, could not find the product ID".format(gallery_link))

    feed_galleries_data = [x for x in feed_galleries_data if x["uid"] is not None]
    gallery_gids = [x["uid"] for x in feed_galleries_data]
    n_containers = len(feed_galleries_data)
    if not gallery_gids:
        stats.error(
            "For provider {}: Got to url: {}, but could not parse the response into galleries, stopping. Number of gallery containers found: {}.".format(
                constants.provider_name, full_url, n_containers
            )
        )
        return
    # Listen to what the server says
    used = ProcessedLinks.objects.filter(source_id__in=gallery_gids, provider=provider)
    # If the amount of galleries present in database is equal to what we get from the page,
    # we assume we already processed everything. The source's force_process processes everything anyway.
    force_process = source.force_process
    logger.info(
        "Page has {} galleries, from which {} are already present in the database.".format(
            len(gallery_gids), used.count()
        )
    )
    if not force_process and used.count() == len(gallery_gids):
        logger.info("Page {} has already been processed entirely, stopping".format(full_url))
        return
    used_gids = used.values_list("source_id", flat=True)
    for gallery_index, gallery_data in enumerate(feed_galleries_data):
        if gallery_data["uid"] in used_gids and not force_process:
            continue

        processed_link, link_created = ProcessedLinks.objects.get_or_create(
            provider=provider,
            source_id=gallery_data["uid"],
            defaults={
                "url": gallery_data["link"],
                "link_date": gallery_data["pubDate"],
                "content": gallery_data["content"],
                "title": gallery_data["title"],
            },
        )

        if not link_created and not force_process:
            stats.error(
                "For provider {}: Got to url: {}, was created before it was processed.".format(
                    constants.provider_name, gallery_data["link"]
                )
            )
            continue

        if not gallery_data["title"]:
            stats.error(
                "For provider {}: Got to url: {}, the title was empty.".format(
                    constants.provider_name, gallery_data["link"]
                )
            )
            continue

        response = provider_web_request(constants.provider_name, own_settings.wait_timer)(
            gallery_data["link"],
            request_dict,
            post=False,
        )

        on_sale_date = None

        if not response:
            stats.error(
                "For provider {}: URL: {}, did not give a response, skipping adding artist".format(
                    constants.provider_name, gallery_data["link"]
                )
            )
        else:
            soup = BeautifulSoup(response.text, "html.parser")

            gallery_data["artists"] = get_artists_from_soup(soup)

            on_sale_date = get_on_sale_date_from_soup(soup)

            cover_image = get_cover_image_from_soup(soup)
            if cover_image:
                gallery_data["image"] = cover_image

        stats.links += 1
        wanted_galleries = get_or_create_wanted_galleries_from_gallery_data(source, stats, gallery_data)

        for wanted_gallery in wanted_galleries:

            mention, mention_created = wanted_gallery.mentions.get_or_create(
                mention_date=gallery_data["pubDate"],
                release_date=on_sale_date,
                type="added_date",
                source=constants.provider_name,
                comment=gallery_data["link"],
            )
            if mention_created:
                stats.mentions += 1
            if mention_created and gallery_data["image"]:
                mention.save_img(gallery_data["image"])
                if on_sale_date:
                    wanted_gallery.calculate_nearest_release_date()


def process_products_page(source, stats, own_settings, provider, request_dict, subpath):
    stop_page = source.params.get("stop_page") or 1
    current_page = 0
    force_process = source.force_process

    total_galleries_data = []
    total_used_ids = []
    total_galleries_to_process = 0

    logger.info("Force stopping at page: {}".format(stop_page))

    while current_page < stop_page:
        current_page += 1
        full_url = "{}/page/{}/".format(subpath.rstrip("/"), current_page)

        link = urllib.parse.urljoin(constants.main_url, full_url)

        response = provider_web_request(constants.provider_name, own_settings.wait_timer)(
            link,
            request_dict,
            post=False,
        )

        if not response:
            stats.error(
                "For provider {}: URL: {}, did not give a response, stopping".format(constants.provider_name, link)
            )
            break

        products_galleries_data = []

        response.encoding = "utf-8"

        products_soup = BeautifulSoup(response.text, "html.parser")

        for product_page in products_soup.select("div.wci-card a.wci-cover-link"):
            product_link = product_page.get("href")
            if not isinstance(product_link, str):
                continue
            product_id_match = PRODUCT_ID_MATCHER.search(product_link)

            if product_id_match:
                product_uid = product_id_match.group(1)
                products_galleries_data.append(
                    {
                        "uid": product_uid,
                        "link": product_link,
                    }
                )
        products_galleries_data = [x for x in products_galleries_data if x["uid"] is not None]
        gallery_gids = [x["uid"] for x in products_galleries_data]
        n_containers = len(products_galleries_data)
        if not gallery_gids:
            stats.error(
                "For provider {}: Got to url: {}, but could not parse the response into galleries, stopping. Number of gallery containers found: {}.".format(
                    constants.provider_name, link, n_containers
                )
            )
            break
        # Listen to what the server says
        used = ProcessedLinks.objects.filter(source_id__in=gallery_gids, provider=provider)
        # If the amount of galleries present in database is equal to what we get from the page,
        # we assume we already processed everything. You can force to process everything by using:

        logger.info(
            "Page {} has {} galleries, from which {} are already present in the database.".format(
                link, len(gallery_gids), used.count()
            )
        )
        total_galleries_to_process += len(gallery_gids) - used.count()
        if not force_process and used.count() == len(gallery_gids):
            logger.info("Page {} has already been processed entirely, stopping".format(link))
            break
        used_gids = used.values_list("source_id", flat=True)

        total_galleries_data.extend(products_galleries_data)
        total_used_ids.extend(used_gids)

    logger.info("Total galleries to process: {}".format(total_galleries_to_process))

    for gallery_index, gallery_data in enumerate(total_galleries_data):
        if gallery_data["uid"] in total_used_ids and not force_process:
            continue

        response = provider_web_request(constants.provider_name, own_settings.wait_timer)(
            gallery_data["link"],
            request_dict,
            post=False,
        )

        if not response:
            stats.error(
                "For provider {}: URL: {}, did not give a response, skipping URL".format(
                    constants.provider_name, gallery_data["link"]
                )
            )
            continue
        else:
            gallery_product_data = parse_product_page(response.text)
            if gallery_product_data is None:
                stats.error(
                    "For provider {}: Got to url: {}, could not parse product page response.".format(
                        constants.provider_name, gallery_data["link"]
                    )
                )
                continue

        processed_link, link_created = ProcessedLinks.objects.get_or_create(
            provider=provider,
            source_id=gallery_product_data["uid"],
            defaults={
                "url": gallery_product_data["link"],
                "link_date": gallery_product_data["pub_date"],
                "content": gallery_product_data["content"],
                "title": gallery_product_data["title"],
            },
        )

        if not link_created and not force_process:
            stats.error(
                "For provider {}: Got to url: {}, was created before it was processed.".format(
                    constants.provider_name, gallery_product_data["link"]
                )
            )
            continue

        if not gallery_product_data["title"]:
            stats.error(
                "For provider {}: Got to url: {}, the title was empty.".format(
                    constants.provider_name, gallery_product_data["link"]
                )
            )
            continue

        stats.links += 1
        wanted_galleries = get_or_create_wanted_galleries_from_gallery_data(source, stats, gallery_product_data)

        for wanted_gallery in wanted_galleries:

            mention, mention_created = wanted_gallery.mentions.get_or_create(
                mention_date=gallery_product_data["pub_date"],
                release_date=gallery_product_data["on_sale_date"],
                type="added_date",
                source=constants.provider_name,
                comment=gallery_product_data["link"],
            )
            if mention_created:
                stats.mentions += 1
            if mention_created and gallery_product_data["image"]:
                mention.save_img(gallery_product_data["image"])
                if gallery_product_data["on_sale_date"]:
                    wanted_gallery.calculate_nearest_release_date()


def get_or_create_wanted_galleries_from_gallery_data(source, stats, gallery_data):
    search_title = format_title_to_wanted_search(gallery_data["title"])
    wanted_galleries = WantedGallery.objects.filter(title=gallery_data["title"], search_title=search_title)

    if not wanted_galleries:
        wanted_gallery = create_wanted_gallery(
            source,
            stats,
            title=gallery_data["title"],
            title_jpn=gallery_data["title"],
            search_title=search_title,
            book_type="Manga",
            page_count=0,
            category="Manga",
            publisher=constants.provider_name,
        )

        for artist_name in gallery_data["artists"]:

            artist_obj = Artist.objects.filter(name=artist_name).first()
            if not artist_obj:
                artist_obj = Artist.objects.create(name=artist_name)
            wanted_gallery.artists.add(artist_obj)
        logger.info(
            "Created wanted gallery ({}): {}, search title: {}".format(
                wanted_gallery.book_type, wanted_gallery.get_absolute_url(), wanted_gallery.search_title
            )
        )

        wanted_galleries = [wanted_gallery]
    else:
        logger.info(
            "For URL: {}, search title: {}, already existed, only adding a mention".format(
                gallery_data["link"], search_title
            )
        )
    return wanted_galleries
