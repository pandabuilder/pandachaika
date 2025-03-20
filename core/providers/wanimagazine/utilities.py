import logging
import re
from datetime import datetime

import pytz
from dateutil import parser as date_parser
import bs4
from bs4 import BeautifulSoup

from core.base.types import DataDict
from .constants import provider_name

logger = logging.getLogger(__name__)


PRODUCT_ID_MATCHER = re.compile(".+?/product/(.+)/$")
ON_SALE_DATE_MATCHER = re.compile(r".+?発売日/(\d+)年(\d+)月(\d+)日")


def map_feed_data_to_internal(feed_data: DataDict) -> DataDict:
    image = ""

    if len(feed_data["content"]) > 0:
        first_content = feed_data["content"][0]["value"]

        content_soup = BeautifulSoup(first_content, "html.parser")

        image_container = content_soup.find("img")

        if isinstance(image_container, bs4.element.Tag):
            if isinstance(image_container["src"], str):
                image = image_container["src"]
    else:
        first_content = ""

    product_id_match = PRODUCT_ID_MATCHER.search(feed_data["link"])

    if product_id_match:
        product_uid = product_id_match.group(1)
    else:
        product_uid = None

    return {
        "uid": product_uid,
        "title": feed_data["title"],
        "link": feed_data["link"],
        "pubDate": date_parser.parse(feed_data["published"]),
        "publisher": feed_data["author"],
        "description": feed_data["summary"],
        "content": first_content,
        "image": image,
        "artist": None,
    }


def parse_product_page(page_content: str) -> DataDict | None:
    soup = BeautifulSoup(page_content, "html.parser")

    url_meta = soup.find("meta", property="og:url")
    title_meta = soup.find("meta", property="og:title")

    if not isinstance(url_meta, bs4.element.Tag) or not isinstance(title_meta, bs4.element.Tag):
        return None

    url = url_meta["content"]
    title = title_meta["content"]

    if not isinstance(url, str):
        return None

    product_id_match = PRODUCT_ID_MATCHER.search(url)

    if product_id_match:
        product_uid = product_id_match.group(1)
    else:
        product_uid = None

    published_date = None

    published_date_container = soup.find("meta", property="article:published_time")

    if (
        published_date_container
        and isinstance(published_date_container, bs4.element.Tag)
        and isinstance(published_date_container["content"], str)
    ):
        published_date = date_parser.parse(published_date_container["content"])

    on_sale_date = get_on_sale_date_from_soup(soup)

    artist = None

    artist_container = soup.select("div.p-product-page__author")

    if artist_container:
        artist_container_sub = artist_container[0].find("a")
        if artist_container_sub:
            artist = artist_container_sub.get_text()

    content = None

    content_container = soup.select("div.p-product-page__content")

    if content_container:
        content = content_container[0].decode_contents()

    image = None

    image_container = soup.select("div.p-product-page__figure")

    if image_container:
        image_url_container = image_container[0].find("img")

        if isinstance(image_url_container, bs4.element.Tag):
            image = image_url_container["src"]

    return {
        "uid": product_uid,
        "title": title,
        "link": url,
        "pub_date": published_date,
        "on_sale_date": on_sale_date,
        "publisher": provider_name,
        "description": None,
        "content": content,
        "image": image,
        "artist": artist,
    }


def get_on_sale_date_from_soup(soup):
    on_sale_date = None

    on_sale_container = soup.select("div.p-product-page__price-size")
    if on_sale_container:
        on_sale_text = on_sale_container[0].get_text()
        on_sale_match = ON_SALE_DATE_MATCHER.search(on_sale_text)
        if on_sale_match:
            sale_text_year = int(on_sale_match.group(1))
            sale_text_month = int(on_sale_match.group(2))
            sale_text_day = int(on_sale_match.group(3))
            try:
                on_sale_date = datetime(
                    sale_text_year, sale_text_month, sale_text_day, 0, 0, 0, 0, pytz.timezone("Asia/Tokyo")
                )
            except ValueError:
                logger.error(
                    "Could not parse year: {}, month: {}, day: {} into datetime.".format(
                        sale_text_year, sale_text_month, sale_text_day
                    )
                )
    return on_sale_date
