import logging
import re
from collections.abc import Sequence
from typing import Any

from django.conf import settings
from django.db.models import Exists, OuterRef, Q

logger = logging.getLogger(__name__)
crawler_settings = settings.CRAWLER_SETTINGS

TAG_ID_PREFETCH_LIMIT = 500
EXACT_TAG_LOOKUP_BATCH_SIZE = 200
MAX_TAG_FILTERS = 100


class TooManyTagsError(ValueError):
    """Raised when a search query specifies more tag filters than allowed."""

    def __init__(self, count: int, limit: int) -> None:
        self.count = count
        self.limit = limit
        super().__init__(f"Too many tags: {count} (at most {limit} per request).")


def parse_bool_flag(value: Any) -> bool:
    """Parse a boolean flag from query parameters or dicts, treating '0'/'false'/'no'/'off'/'' as False."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def apply_tag_filters(
    results: Any,
    tags_str: str | Sequence[str],
    *,
    through_model: type[Any] | None = None,
    fk_field: str | None = None,
    through_models: Sequence[tuple[type[Any], str]] | None = None,
    tags_or: bool = False,
    resolve_tag_ids: bool = True,
    max_tags: int | None = None,
) -> Any:
    """Apply tag filters using EXISTS / NOT EXISTS semi-joins instead of multiplying JOINs.

    Pre-resolves matching Tag IDs (up to ``TAG_ID_PREFETCH_LIMIT`` per tag, batching exact
    ``scope:name`` lookups into a single query) so PostgreSQL can probe the through-table index
    directly without joining ``viewer_tag`` or requiring ``SELECT DISTINCT *``.

    Collapses mathematically equivalent clauses:
    - Positive resolved tags in ``tags_or=True`` mode become a single ``EXISTS (... tag_id IN (...))``
      semi-join rather than N correlated ``OR EXISTS`` subplans.
    - Negated resolved tags in ``tags_or=False`` mode become a single ``NOT EXISTS (... tag_id IN (...))``
      anti-join (by De Morgan's law).
    """
    from viewer.models import Tag

    if through_models is not None:
        targets = list(through_models)
    elif through_model is not None and fk_field is not None:
        targets = [(through_model, fk_field)]
    else:
        raise ValueError("Either through_model+fk_field or through_models must be provided.")

    raw_tags = tags_str.split(",") if isinstance(tags_str, str) else list(tags_str)
    non_empty_tags = [t for t in (raw_tag.strip().replace(" ", "_") for raw_tag in raw_tags) if t]
    if not non_empty_tags:
        return results

    if max_tags is not None and len(non_empty_tags) > max_tags:
        raise TooManyTagsError(len(non_empty_tags), max_tags)

    specs: list[tuple[bool, bool, str, str]] = []
    seen_specs: set[tuple[bool, bool, str, str]] = set()
    for tag in non_empty_tags:
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
        if tag_name == "" and tag_scope == "":
            continue
        spec = (negated, exact, tag_scope, tag_name)
        if spec not in seen_specs:
            seen_specs.add(spec)
            specs.append(spec)

    if not specs:
        return results

    exact_scoped_ids: dict[tuple[str, str], list[int]] = {}
    if resolve_tag_ids:
        exact_scoped_pairs: list[tuple[str, str]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for _negated, exact, tag_scope, tag_name in specs:
            if exact and tag_name != "" and tag_scope != "":
                pair = (tag_scope, tag_name)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    exact_scoped_pairs.append(pair)
        if len(exact_scoped_pairs) >= 2:
            exact_scoped_ids = {pair: [] for pair in exact_scoped_pairs}
            for start in range(0, len(exact_scoped_pairs), EXACT_TAG_LOOKUP_BATCH_SIZE):
                chunk = exact_scoped_pairs[start : start + EXACT_TAG_LOOKUP_BATCH_SIZE]
                batch_q = Q()
                for scope, name in chunk:
                    batch_q.add(Q(scope=scope, name=name), Q.OR)
                for scope, name, pk in Tag.objects.filter(batch_q).values_list("scope", "name", "pk"):
                    key = (scope, name)
                    if key in exact_scoped_ids:
                        exact_scoped_ids[key].append(pk)

    def _exists_for_ids(tag_ids_list: list[int]) -> Q:
        tag_filter: dict[str, Any] = (
            {"tag_id": tag_ids_list[0]} if len(tag_ids_list) == 1 else {"tag_id__in": tag_ids_list}
        )
        exists_q = Q()
        for model, field in targets:
            subquery = model.objects.filter(**{field: OuterRef("pk"), **tag_filter})
            exists_q.add(Q(Exists(subquery)), Q.OR)
        return exists_q

    def _exists_for_through_q(through_tag_q: Q) -> Q:
        exists_q = Q()
        for model, field in targets:
            subquery = model.objects.filter(**{field: OuterRef("pk")}).filter(through_tag_q)
            exists_q.add(Q(Exists(subquery)), Q.OR)
        return exists_q

    or_q = Q()
    has_or_clause = False
    or_positive_tag_ids: list[int] = []
    or_positive_tag_id_set: set[int] = set()
    and_negative_tag_ids: list[int] = []
    and_negative_tag_id_set: set[int] = set()

    for negated, exact, tag_scope, tag_name in specs:
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
            if exact and tag_name != "" and tag_scope != "" and (tag_scope, tag_name) in exact_scoped_ids:
                tag_ids = exact_scoped_ids[(tag_scope, tag_name)]
            else:
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
            if tags_or and not negated:
                for pk in tag_ids:
                    if pk not in or_positive_tag_id_set:
                        or_positive_tag_id_set.add(pk)
                        or_positive_tag_ids.append(pk)
                has_or_clause = True
                continue
            if not tags_or and negated:
                for pk in tag_ids:
                    if pk not in and_negative_tag_id_set:
                        and_negative_tag_id_set.add(pk)
                        and_negative_tag_ids.append(pk)
                continue
            exists_q = _exists_for_ids(tag_ids)
        else:
            exists_q = _exists_for_through_q(through_tag_q)

        clause = ~exists_q if negated else exists_q

        if tags_or:
            or_q.add(clause, Q.OR)
            has_or_clause = True
        else:
            results = results.filter(clause)

    if tags_or:
        if or_positive_tag_ids:
            or_q.add(_exists_for_ids(or_positive_tag_ids), Q.OR)
        if or_q:
            results = results.filter(or_q)
        elif has_or_clause:
            return results.none()
    elif and_negative_tag_ids:
        results = results.filter(~_exists_for_ids(and_negative_tag_ids))

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
    "expunged",
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



