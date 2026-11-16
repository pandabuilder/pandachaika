import json
import logging
import re
from datetime import datetime
import zoneinfo

from dateutil import parser as date_parser
import bs4
from bs4 import BeautifulSoup

from core.base.types import DataDict
from .constants import provider_name

logger = logging.getLogger(__name__)


# Since the September 2026 site renewal, works are custom post types: /comics/<ISBN>/, /magazine/<slug>/, etc.
# /product/<slug>/ was the WooCommerce URL used before that.
PRODUCT_ID_MATCHER = re.compile(r"/(?:product|comics|magazine|goods_adult|goods_common|full_color|books)/([^/?#]+)/")
SALE_DATE_MATCHER = re.compile(r"(\d+)年(\d+)月(\d+)日")
# Cover shown for works that still don't have one, not worth saving as a mention image.
PLACEHOLDER_IMAGE_MARKER = "now-printing"


def is_placeholder_image(image_url: str | None) -> bool:
    return not image_url or PLACEHOLDER_IMAGE_MARKER in image_url


def map_feed_data_to_internal(feed_data: DataDict) -> DataDict:
    image = ""

    if len(feed_data.get("content", [])) > 0:
        first_content = feed_data["content"][0]["value"]

        content_soup = BeautifulSoup(first_content, "html.parser")

        image_container = content_soup.find("img")

        if isinstance(image_container, bs4.element.Tag):
            if isinstance(image_container["src"], str):
                image = image_container["src"]
    else:
        first_content = ""

    if is_placeholder_image(image):
        image = ""

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
        "publisher": feed_data.get("author", ""),
        "description": feed_data.get("summary", ""),
        "content": first_content,
        "image": image,
        "artists": [],
    }


def get_artists_from_soup(soup: BeautifulSoup) -> list[str]:
    artists = []
    for author_link in soup.select("div.wmp-author a.wmp-author-link"):
        author_name = author_link.get_text(strip=True)
        if author_name and author_name not in artists:
            artists.append(author_name)
    return artists


def get_cover_image_from_soup(soup: BeautifulSoup) -> str | None:
    image_container = soup.select_one("img.wmp-cover-img")
    if image_container is None:
        return None
    image = image_container.get("src")
    if not isinstance(image, str) or is_placeholder_image(image):
        return None
    return image


def get_published_date_from_soup(soup: BeautifulSoup) -> datetime | None:
    published_date_container = soup.find("meta", property="article:published_time")
    if isinstance(published_date_container, bs4.element.Tag):
        published_date_text = published_date_container.get("content")
        if isinstance(published_date_text, str):
            return date_parser.parse(published_date_text)

    # The renewed site only exposes it in the JSON-LD Article node.
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            ld_data = json.loads(script.get_text())
        except ValueError:
            continue
        nodes = ld_data.get("@graph", [ld_data]) if isinstance(ld_data, dict) else ld_data
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") == "Article" and node.get("datePublished"):
                try:
                    return date_parser.parse(node["datePublished"])
                except (ValueError, OverflowError):
                    return None
    return None


def parse_product_page(page_content: str) -> DataDict | None:
    soup = BeautifulSoup(page_content, "html.parser")

    url_meta = soup.find("meta", property="og:url")

    if not isinstance(url_meta, bs4.element.Tag):
        return None

    url = url_meta["content"]

    if not isinstance(url, str):
        return None

    title = None
    title_container = soup.select_one("h1.wmp-title")
    if title_container:
        title = title_container.get_text(strip=True)
    if not title:
        # og:title is "<title>｜<author>｜<label>｜ワニマガweb".
        title_meta = soup.find("meta", property="og:title")
        if isinstance(title_meta, bs4.element.Tag) and isinstance(title_meta["content"], str):
            title = title_meta["content"].split("｜")[0].strip()
    if not title:
        return None

    product_id_match = PRODUCT_ID_MATCHER.search(url)

    if product_id_match:
        product_uid = product_id_match.group(1)
    else:
        product_uid = None

    content = ""

    content_container = soup.select_one("div.wmp-biblio-body")

    if content_container:
        content = content_container.decode_contents()

    return {
        "uid": product_uid,
        "title": title,
        "link": url,
        "pub_date": get_published_date_from_soup(soup),
        "on_sale_date": get_on_sale_date_from_soup(soup),
        "publisher": provider_name,
        "description": None,
        "content": content,
        "image": get_cover_image_from_soup(soup),
        "artists": get_artists_from_soup(soup),
    }


def get_on_sale_date_from_soup(soup: BeautifulSoup) -> datetime | None:
    # Spec table rows: 紙版発売日 (print), 電子版発売日 (digital), 他の電子書店 (other stores). Prefer print.
    sale_dates: dict[str, str] = {}
    for row in soup.select("table.wmp-spec tr"):
        header = row.find("th")
        value = row.find("td")
        if header is None or value is None:
            continue
        header_text = header.get_text(strip=True)
        if "発売日" in header_text:
            sale_dates[header_text] = value.get_text(strip=True)

    if not sale_dates:
        return None

    on_sale_text = sale_dates.get("紙版発売日") or sale_dates.get("発売日") or next(iter(sale_dates.values()))

    on_sale_match = SALE_DATE_MATCHER.search(on_sale_text)
    if not on_sale_match:
        return None

    sale_text_year = int(on_sale_match.group(1))
    sale_text_month = int(on_sale_match.group(2))
    sale_text_day = int(on_sale_match.group(3))
    try:
        return datetime(sale_text_year, sale_text_month, sale_text_day, 0, 0, 0, 0, zoneinfo.ZoneInfo("Asia/Tokyo"))
    except ValueError:
        logger.error(
            "Could not parse year: {}, month: {}, day: {} into datetime.".format(
                sale_text_year, sale_text_month, sale_text_day
            )
        )
        return None
