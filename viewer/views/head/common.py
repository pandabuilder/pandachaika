import logging
import re
from typing import Any

from django.conf import settings
from django.db.models import Exists, OuterRef, Q

logger = logging.getLogger(__name__)
crawler_settings = settings.CRAWLER_SETTINGS

TAG_ID_PREFETCH_LIMIT = 500


def apply_tag_filters(
    results: Any,
    tags_str: str,
    *,
    through_model: type[Any],
    fk_field: str,
    tags_or: bool = False,
    resolve_tag_ids: bool = True,
) -> Any:
    """Apply comma-separated tag filters using EXISTS / NOT EXISTS semi-joins instead of multiplying JOINs.

    Pre-resolves matching Tag IDs (up to ``TAG_ID_PREFETCH_LIMIT``) so PostgreSQL can probe the
    through-table index directly without joining ``viewer_tag`` or requiring ``SELECT DISTINCT *``.
    """
    from viewer.models import Tag

    raw_tags = tags_str.split(",")
    or_q = Q()
    has_or_clause = False

    for raw_tag in raw_tags:
        tag = raw_tag.strip().replace(" ", "_")
        negated = tag.startswith("-")
        exact = tag.startswith("^") or tag.startswith("-^")
        tag_clean = re.sub("^[-|^]", "", tag)
        scope_name = tag_clean.split(":", maxsplit=1)
        if len(scope_name) > 1:
            tag_scope = scope_name[0]
            tag_name = scope_name[1]
        else:
            tag_scope = ""
            tag_name = scope_name[0]

        lookup = "exact" if exact else "contains"
        if tag_name != "" and tag_scope != "":
            tag_q = Q(**{f"name__{lookup}": tag_name}) & Q(**{f"scope__{lookup}": tag_scope})
            through_tag_q = Q(**{f"tag__name__{lookup}": tag_name}) & Q(**{f"tag__scope__{lookup}": tag_scope})
        elif tag_name != "":
            tag_q = Q(**{f"name__{lookup}": tag_name})
            through_tag_q = Q(**{f"tag__name__{lookup}": tag_name})
        else:
            tag_q = Q(**{f"scope__{lookup}": tag_scope})
            through_tag_q = Q(**{f"tag__scope__{lookup}": tag_scope})

        tag_ids: list[int] | None = None
        if resolve_tag_ids:
            tag_ids = list(Tag.objects.filter(tag_q).values_list("pk", flat=True)[: TAG_ID_PREFETCH_LIMIT + 1])

        if tag_ids is not None and len(tag_ids) == 0:
            if tags_or:
                if negated:
                    return results
                has_or_clause = True
                continue
            if negated:
                continue
            return results.none()

        if tag_ids is not None and len(tag_ids) <= TAG_ID_PREFETCH_LIMIT:
            if len(tag_ids) == 1:
                subquery = through_model.objects.filter(**{fk_field: OuterRef("pk"), "tag_id": tag_ids[0]})
            else:
                subquery = through_model.objects.filter(**{fk_field: OuterRef("pk"), "tag_id__in": tag_ids})
        else:
            subquery = through_model.objects.filter(**{fk_field: OuterRef("pk")}).filter(through_tag_q)

        clause = ~Q(Exists(subquery)) if negated else Q(Exists(subquery))

        if tags_or:
            or_q.add(clause, Q.OR)
            has_or_clause = True
        else:
            results = results.filter(clause)

    if tags_or:
        if or_q:
            results = results.filter(or_q)
        elif has_or_clause:
            return results.none()

    return results

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



