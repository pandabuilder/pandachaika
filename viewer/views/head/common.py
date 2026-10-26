import logging
import typing
from typing import Any
from django.conf import settings
from core.base.types import DataDict

logger = logging.getLogger(__name__)
crawler_settings = settings.CRAWLER_SETTINGS

gallery_filter_keys = [
    "title",
    "rating_from",
    "rating_to",
    "filesize_from",
    "filesize_to",
    "filecount_from",
    "filecount_to",
    "posted_from",
    "posted_to",
    "create_from",
    "create_to",
    "category",
    "categories",
    "provider",
    "providers",
    "dl_type",
    "expunged",
    "disowned",
    "hidden",
    "fjord",
    "uploader",
    "tags",
    "not_used",
    "not_used_including_groups",
    "reason",
    "only_used",
    "contains",
    "not_contains",
    "contained",
    "contained_container",
    "contained_magazine",
    "not_contained",
    "not_contained_container",
    "not_contained_magazine",
    "not_normal",
    "crc32",
    "used",
    "public",
    "not_public",
    "comment",
    "empty_comment",
    "tags_or",
]

gallery_list_filters_keys = [
    "categories",
    "providers",
]

archive_filter_keys = [
    "title",
    "filename",
    "filecount_from",
    "filecount_to",
    "filesize_from",
    "filesize_to",
    "rating_from",
    "rating_to",
    "match_type",
    "posted_from",
    "posted_to",
    "source_type",
    "tags",
    "only_favorites",
    "non_public",
    "public",
    "reason",
    "uploader",
    "category",
    "provider",
    "hidden",
    "qsearch",
    "extracted",
]

archive_order_fields = [
    "title",
    "title_jpn",
    "rating",
    "filesize",
    "filecount",
    "posted",
    "create_date",
    "public_date",
    "last_modified",
    "category",
    "reason",
    "source_type",
    "gallery__posted",
    "gallery__uploader",
    "gallery__category",
]

archive_public_order_fields = [
    "title",
    "title_jpn",
    "rating",
    "filesize",
    "filecount",
    "posted",
    "public_date",
    "category",
    "reason",
    "source_type",
    "gallery__posted",
    "gallery__uploader",
    "gallery__category",
]

archive_sort_by_fields = [
    "title",
    "title_jpn",
    "filesize",
    "filecount",
    "public_date",
    "reason",
    "source_type",
    "create_date",
    "last_modified",
    "gallery__category",
    "gallery__posted",
    "gallery__uploader",
    "gallery__category",
]

archive_public_sort_by_fields = [
    "title",
    "title_jpn",
    "filesize",
    "filecount",
    "public_date",
    "reason",
    "source_type",
    "gallery__category",
    "gallery__posted",
    "gallery__uploader",
    "gallery__category",
]

gallery_order_fields = (
    "title",
    "title_jpn",
    "rating",
    "filesize",
    "filecount",
    "posted",
    "create_date",
    "category",
    "provider",
    "uploader",
)

wanted_gallery_filter_keys = (
    "title",
    "wanted_page_count_lower",
    "wanted_page_count_upper",
    "provider",
    "not_used",
    "wanted-should-search",
    "wanted-should-search-not",
    "book_type",
    "publisher",
    "wanted-found",
    "wanted-not-found",
    "reason",
    "wanted-no-found-galleries",
    "with-possible-matches",
    "tags",
    "mention-source",
    "restricted-to-links",
)

wanted_gallery_order_fields = ("title", "date_found", "create_date", "last_modified", "release_date")



