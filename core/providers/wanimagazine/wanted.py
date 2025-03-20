import logging
import time
import typing
import urllib.parse

import feedparser
from bs4 import BeautifulSoup

from core.base.types import DataDict
from core.base.utilities import request_with_retries, format_title_to_wanted_search, construct_request_dict
from viewer.models import WantedGallery, Provider, Artist, ProcessedLinks
from . import constants, utilities
from .utilities import get_on_sale_date_from_soup, PRODUCT_ID_MATCHER, parse_product_page

if typing.TYPE_CHECKING:
    from core.base.setup import Settings
    from viewer.models import AttributeManager

logger = logging.getLogger(__name__)


def wanted_generator(settings: "Settings", attrs: "AttributeManager"):
    own_settings = settings.providers[constants.provider_name]

    queries: DataDict = {}

    for attr in attrs.filter(name__startswith="wanted_params_"):

        attr_info = attr.name.replace("wanted_params_", "")
        query_name, attr_name = attr_info.split("_", maxsplit=1)

        if query_name not in queries:
            queries[query_name] = {}

        queries[query_name].update({attr_name: attr.value})

    provider, provider_created = Provider.objects.get_or_create(
        slug=constants.provider_name, defaults={"name": constants.provider_name}
    )

    rounds = 0

    # Values that can be set:
    # subpath: RSS Feed subpath to search. (product-category/comic/feed/, product-category/comic/)
    # url_html_type: If it's RSS feed or products list page.
    for query_name, query_values in queries.items():

        rounds += 1

        if rounds > 1:
            time.sleep(own_settings.wait_timer)

        logger.info(
            "Querying {} for auto wanted galleries, query name: {}, options: {}".format(
                constants.provider_name, query_name, str(query_values)
            )
        )

        if "subpath" not in query_values:
            logger.error("Cannot query without setting a subpath for {}".format(query_name))
            continue
        subpath = query_values["subpath"]

        if "url_html_type" in query_values:
            url_html_type = query_values["url_html_type"]
        else:
            url_html_type = False

        request_dict = construct_request_dict(settings, own_settings)

        if url_html_type:
            if "cookies" not in request_dict:
                request_dict["cookies"] = {}
            request_dict["cookies"]["isOver18"] = "true"
            process_products_page(attrs, own_settings, provider, query_name, request_dict, subpath)
        else:
            process_feed_page(attrs, own_settings, provider, query_name, request_dict, subpath)

    logger.info("{} Auto wanted ended.".format(constants.provider_name))


def process_feed_page(attrs, own_settings, provider, query_name, request_dict, subpath):
    full_url = "{}".format(subpath)

    gallery_gids = []

    link = urllib.parse.urljoin(constants.main_url, full_url)

    response = request_with_retries(
        link,
        request_dict,
        post=False,
    )

    if not response:
        logger.error(
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
            logger.error("Skipping link: {}, could not find the product ID".format(gallery_link))

    feed_galleries_data = [x for x in feed_galleries_data if x["uid"] is not None]
    gallery_gids = [x["uid"] for x in feed_galleries_data]
    n_containers = len(feed_galleries_data)
    if not gallery_gids:
        logger.error(
            "For provider {}: Got to url: {}, but could not parse the response into galleries, stopping. Number of gallery containers found: {}.".format(
                constants.provider_name, full_url, n_containers
            )
        )
        return
    # Listen to what the server says
    used = ProcessedLinks.objects.filter(source_id__in=gallery_gids, provider=provider)
    # If the amount of galleries present in database is equal to what we get from the page,
    # we assume we already processed everything. You can force to process everything by using:
    force_process, force_created = attrs.get_or_create(
        provider=provider,
        name="force_process",
        data_type="bool",
        defaults={
            "value_bool": False,
        },
    )
    logger.info(
        "Page has {} galleries, from which {} are already present in the database.".format(
            len(gallery_gids), used.count()
        )
    )
    if not force_process.value and used.count() == len(gallery_gids):
        logger.info("Page {} has already been processed entirely, stopping".format(full_url))
        return
    used_gids = used.values_list("source_id", flat=True)
    for gallery_index, gallery_data in enumerate(feed_galleries_data):
        if gallery_data["uid"] in used_gids and not force_process.value:
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

        if not link_created and not not force_process.value:
            logger.error(
                "For provider {}: Got to url: {}, was created before it was processed.".format(
                    constants.provider_name, gallery_data["link"]
                )
            )
            continue

        if not gallery_data["title"]:
            logger.error(
                "For provider {}: Got to url: {}, the title was empty.".format(
                    constants.provider_name, gallery_data["link"]
                )
            )
            continue

        if gallery_index > 1:
            time.sleep(own_settings.wait_timer)

        response = request_with_retries(
            gallery_data["link"],
            request_dict,
            post=False,
        )

        on_sale_date = None

        if not response:
            logger.error(
                "For provider {}: URL: {}, did not give a response, skipping adding artist".format(
                    constants.provider_name, gallery_data["link"]
                )
            )
        else:
            soup = BeautifulSoup(response.text, "html.parser")

            author_containers = soup.select("div.p-product-page__author")

            if len(author_containers) >= 1:
                author = author_containers[0].get_text()
                gallery_data["artist"] = author

            on_sale_date = get_on_sale_date_from_soup(soup)

        wanted_galleries = get_or_create_wanted_galleries_from_gallery_data(attrs, gallery_data, query_name)

        for wanted_gallery in wanted_galleries:

            mention, mention_created = wanted_gallery.mentions.get_or_create(
                mention_date=gallery_data["pubDate"],
                release_date=on_sale_date,
                type="added_date",
                source=constants.provider_name,
                comment=gallery_data["link"],
            )
            if mention_created and gallery_data["image"]:
                mention.save_img(gallery_data["image"])
                if on_sale_date:
                    wanted_gallery.calculate_nearest_release_date()


def process_products_page(attrs, own_settings, provider, query_name, request_dict, subpath):
    stop_page = attrs.fetch_value("stop_page_{}".format(query_name))
    current_page = 0

    if not stop_page:
        stop_page = 1

    force_process, force_created = attrs.get_or_create(
        provider=provider,
        name="force_process",
        data_type="bool",
        defaults={
            "value_bool": False,
        },
    )

    total_galleries_data = []
    total_used_ids = []
    total_galleries_to_process = 0

    logger.info("Force stopping at page: {}".format(stop_page))

    while current_page < stop_page:
        current_page += 1
        full_url = "{}/page/{}".format(subpath, current_page)

        link = urllib.parse.urljoin(constants.main_url, full_url)

        if current_page > 1:
            time.sleep(own_settings.wait_timer)

        response = request_with_retries(
            link,
            request_dict,
            post=False,
        )

        if not response:
            logger.error(
                "For provider {}: URL: {}, did not give a response, stopping".format(constants.provider_name, link)
            )
            break

        products_galleries_data = []

        response.encoding = "utf-8"

        products_soup = BeautifulSoup(response.text, "html.parser")

        product_container = products_soup.select("div.p-products")

        if product_container:
            li_containers = product_container[0].select("li.p-products__item")
            if li_containers:
                for li_container in li_containers:
                    product_page = li_container.find("a")
                    if product_page:
                        product_link = product_page["href"]
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
            logger.error(
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
        if not force_process.value and used.count() == len(gallery_gids):
            logger.info("Page {} has already been processed entirely, stopping".format(link))
            break
        used_gids = used.values_list("source_id", flat=True)

        total_galleries_data.extend(products_galleries_data)
        total_used_ids.extend(used_gids)

    logger.info("Total galleries to process: {}".format(total_galleries_to_process))

    for gallery_index, gallery_data in enumerate(total_galleries_data):
        if gallery_data["uid"] in total_used_ids and not force_process.value:
            continue

        if gallery_index > 1:
            time.sleep(own_settings.wait_timer)

        response = request_with_retries(
            gallery_data["link"],
            request_dict,
            post=False,
        )

        if not response:
            logger.error(
                "For provider {}: URL: {}, did not give a response, skipping URL".format(
                    constants.provider_name, gallery_data["link"]
                )
            )
            continue
        else:
            gallery_product_data = parse_product_page(response.text)
            if gallery_product_data is None:
                logger.error(
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

        if not link_created and not force_process.value:
            logger.error(
                "For provider {}: Got to url: {}, was created before it was processed.".format(
                    constants.provider_name, gallery_product_data["link"]
                )
            )
            continue

        if not gallery_product_data["title"]:
            logger.error(
                "For provider {}: Got to url: {}, the title was empty.".format(
                    constants.provider_name, gallery_product_data["link"]
                )
            )
            continue

        wanted_galleries = get_or_create_wanted_galleries_from_gallery_data(attrs, gallery_product_data, query_name)

        for wanted_gallery in wanted_galleries:

            mention, mention_created = wanted_gallery.mentions.get_or_create(
                mention_date=gallery_product_data["pub_date"],
                release_date=gallery_product_data["on_sale_date"],
                type="added_date",
                source=constants.provider_name,
                comment=gallery_product_data["link"],
            )
            if mention_created and gallery_product_data["image"]:
                mention.save_img(gallery_product_data["image"])
                if gallery_product_data["on_sale_date"]:
                    wanted_gallery.calculate_nearest_release_date()


def get_or_create_wanted_galleries_from_gallery_data(attrs, gallery_data, query_name):
    search_title = format_title_to_wanted_search(gallery_data["title"])
    wanted_galleries = WantedGallery.objects.filter(title=gallery_data["title"], search_title=search_title)

    if not wanted_galleries:

        wanted_reason = attrs.fetch_value("wanted_reason_{}".format(query_name))

        if isinstance(wanted_reason, str):
            new_wanted_reason = wanted_reason
        else:
            new_wanted_reason = ""

        new_public = attrs.fetch_value("wanted_public_{}".format(query_name))

        if isinstance(new_public, bool):
            new_public = new_public
        else:
            new_public = False

        new_should_search = attrs.fetch_value("wanted_should_search_{}".format(query_name))

        if isinstance(new_should_search, bool):
            new_should_search = new_should_search
        else:
            new_should_search = False

        new_keep_searching = attrs.fetch_value("wanted_keep_searching_{}".format(query_name))

        if isinstance(new_keep_searching, bool):
            new_keep_searching = new_keep_searching
        else:
            new_keep_searching = False

        new_wanted_notify_when_found = attrs.fetch_value("wanted_notify_when_found_{}".format(query_name))

        if isinstance(new_wanted_notify_when_found, bool):
            new_wanted_notify_when_found = new_wanted_notify_when_found
        else:
            new_wanted_notify_when_found = False

        wanted_gallery = WantedGallery.objects.create(
            title=gallery_data["title"],
            title_jpn=gallery_data["title"],
            search_title=search_title,
            book_type="Manga",
            page_count=0,
            category="Manga",
            publisher=constants.provider_name,
            reason=new_wanted_reason,
            public=new_public,
            should_search=new_should_search,
            keep_searching=new_keep_searching,
            notify_when_found=new_wanted_notify_when_found,
        )
        wanted_provider_string = attrs.fetch_value("wanted_provider_{}".format(query_name))
        if wanted_provider_string and isinstance(wanted_provider_string, str):
            wanted_provider_instance = Provider.objects.filter(slug=wanted_provider_string).first()
            if wanted_provider_instance:
                wanted_gallery.wanted_providers.add(wanted_provider_instance)
        wanted_providers_string = attrs.fetch_value("wanted_providers_{}".format(query_name))
        if wanted_providers_string and isinstance(wanted_providers_string, str):
            for wanted_provider in wanted_providers_string.split():
                wanted_provider = wanted_provider.strip()
                wanted_provider_instance = Provider.objects.filter(slug=wanted_provider).first()
                if wanted_provider_instance:
                    wanted_gallery.wanted_providers.add(wanted_provider_instance)

        if gallery_data["artist"]:

            artist_obj = Artist.objects.filter(name=gallery_data["artist"]).first()
            if not artist_obj:
                artist_obj = Artist.objects.create(name=gallery_data["artist"])
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
