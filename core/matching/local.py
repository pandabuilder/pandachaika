"""Offline matchers: compare a target with the galleries already in the local database.

Each one is scoped to a set of gallery providers (substring filters, like the "provider filter" in the UI). No
requests are made.
"""

import logging
import re
import typing
from collections.abc import Callable, Sequence
from difflib import SequenceMatcher
from functools import cached_property

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from core.base.comparison import get_list_closer_text_from_list
from core.base.utilities import clean_title, get_title_from_path, replace_illegal_name
from core.matching.types import MatchCandidate, MatchTarget, pick_best

if typing.TYPE_CHECKING:
    from viewer.models import GalleryQuerySet

logger = logging.getLogger(__name__)

# Order matters for auto-link: the first method with a good enough candidate wins.
LOCAL_METHODS = ("crc32", "gid", "image", "thumbnail", "title", "size")


def _similarity(text: str, titles: Sequence[str | None]) -> float:
    if not text:
        return 0.0
    return max((SequenceMatcher(None, text, t).ratio() for t in titles if t), default=0.0)


class GalleryScope:
    """Which local galleries a local matcher compares with. No providers means all of them."""

    def __init__(self, providers: Sequence[str] = (), must_be_used: bool = False) -> None:
        self.providers = tuple(p for p in providers if p)
        # Only galleries that have an archive (directly, or through their container or magazine).
        self.must_be_used = must_be_used

    def __str__(self) -> str:
        return ",".join(self.providers) or "all"

    def provider_q(self, prefix: str = "") -> Q:
        query = Q()
        for provider in self.providers:
            query |= Q(**{"{}provider__contains".format(prefix): provider})
        return query

    def galleries(self) -> "GalleryQuerySet":
        from viewer.models import Gallery

        galleries = Gallery.objects.eligible_for_use().filter(self.provider_q())
        if self.must_be_used:
            galleries = galleries.filter(
                Q(archive__isnull=False)
                | (Q(gallery_container__isnull=False) & Q(gallery_container__archive__isnull=False))
                | (Q(magazine__isnull=False) & Q(magazine__archive__isnull=False))
            ).distinct()
        return galleries

    def contains_provider(self, provider: str | None) -> bool:
        return not self.providers or any(p in (provider or "") for p in self.providers)


class LocalMatcher:
    name = ""
    type = ""
    provider = "local"
    exact = True
    default_cutoff = 0.0

    def __init__(self, scope: GalleryScope) -> None:
        self.scope = scope

    def __str__(self) -> str:
        return "local_{}[{}]".format(self.name, self.scope)

    @property
    def found_by(self) -> str:
        return "local_{}".format(self.name)

    def find(self, target: MatchTarget, cutoff: float, max_matches: int) -> list[MatchCandidate]:
        raise NotImplementedError

    def best_candidate(self, candidates: list[MatchCandidate], cutoff: float | None = None) -> MatchCandidate | None:
        return pick_best(candidates, self.exact, self.default_cutoff if cutoff is None else cutoff)

    def gallery_candidates(
        self, target: MatchTarget, gallery_ids: typing.Iterable[int], max_matches: int, match_type: str = ""
    ) -> list[MatchCandidate]:
        """Exact candidates for these galleries, ranked by title similarity with the target."""
        compare = get_title_from_path(target.text) if target.text else ""
        candidates = [
            MatchCandidate(
                matcher=str(self),
                match_type=match_type or self.type,
                found_by=self.found_by,
                accuracy=1.0,
                title=title or title_jpn or "",
                gallery_id=pk,
                title_similarity=_similarity(compare, (title, title_jpn)),
            )
            for pk, title, title_jpn in self.scope.galleries()
            .filter(pk__in=set(gallery_ids))
            .values_list("pk", "title", "title_jpn")
        ]
        candidates.sort(key=lambda c: c.title_similarity, reverse=True)
        return candidates[:max_matches] if max_matches > 0 else candidates


class LocalTitleMatcher(LocalMatcher):
    """Fuzzy title comparison. The file name is formatted like the provider's title matcher does, when known."""

    name = "title"
    type = "title"
    exact = False
    default_cutoff = 0.7

    def __init__(
        self,
        scope: GalleryScope,
        formatter: Callable[[str], str] | None = None,
        apply_clean_title: bool = False,
        index_normalizer: Callable[[str], str] = replace_illegal_name,
    ) -> None:
        super().__init__(scope)
        self.formatter = formatter
        self.apply_clean_title = apply_clean_title
        # File names can't contain some characters, so gallery titles are compared without them by default.
        self.index_normalizer = index_normalizer

    def compare_titles(self, target: MatchTarget) -> list[str]:
        """The texts compared with the gallery titles: the file name as is, and formatted like the provider does.

        Files are often named exactly like their gallery, which the provider formatting (that removes groups and
        artists, for example) would score lower.
        """
        if not target.text:
            return []
        # A title is used as is, a file path without its folders and extension.
        titles = [target.title or get_title_from_path(target.file_path)]
        if self.formatter:
            titles.append(self.formatter(target.text))
        if self.apply_clean_title:
            titles = [clean_title(x) for x in titles]
        return [x for x in dict.fromkeys(titles) if x]

    @cached_property
    def index(self) -> list[tuple[str, int]]:
        """(title, gallery pk) for every gallery in scope, loaded once per run."""
        titles: list[tuple[str, int]] = []
        for pk, title, title_jpn in self.scope.galleries().values_list("pk", "title", "title_jpn").iterator(
            chunk_size=5000
        ):
            if title:
                titles.append((self.index_normalizer(title), pk))
            if title_jpn:
                titles.append((self.index_normalizer(title_jpn), pk))
        logger.info("Matcher {}: loaded {} titles from local galleries.".format(self, len(titles)))
        return titles

    def find(self, target: MatchTarget, cutoff: float, max_matches: int) -> list[MatchCandidate]:
        similar: list[tuple[str, int, float]] = []
        for compare in self.compare_titles(target):
            similar.extend(get_list_closer_text_from_list(compare, self.index, cutoff, max_matches) or [])
        best_by_gallery: dict[int, MatchCandidate] = {}
        for title, pk, accuracy in similar:
            current = best_by_gallery.get(pk)
            if current is None or accuracy > current.accuracy:
                best_by_gallery[pk] = MatchCandidate(
                    matcher=str(self),
                    match_type=self.type,
                    found_by=self.found_by,
                    accuracy=accuracy,
                    title=title,
                    gallery_id=pk,
                    title_similarity=accuracy,
                )
        return sorted(best_by_gallery.values(), key=lambda c: c.accuracy, reverse=True)[:max_matches]


class LocalGidMatcher(LocalMatcher):
    """Files named like "<gid> <title>"."""

    name = "gid"
    type = "gid"

    def __init__(self, scope: GalleryScope, formatter: Callable[[str], str] | None = None) -> None:
        super().__init__(scope)
        self.formatter = formatter

    def gid_from_target(self, target: MatchTarget) -> str:
        if not target.text:
            return ""
        if self.formatter:
            return self.formatter(target.text)
        result = re.search(r"^(\d+)", clean_title(get_title_from_path(target.text)))
        return result.group(1) if result else ""

    def find(self, target: MatchTarget, cutoff: float, max_matches: int) -> list[MatchCandidate]:
        gid = self.gid_from_target(target)
        if not gid:
            return []
        ids = self.scope.galleries().filter(gid=gid).values_list("pk", flat=True)
        return self.gallery_candidates(target, ids, max_matches)


class LocalSizeMatcher(LocalMatcher):
    name = "size"
    type = "size"

    def find(self, target: MatchTarget, cutoff: float, max_matches: int) -> list[MatchCandidate]:
        if not target.filesize or target.filesize <= 0:
            return []
        ids = self.scope.galleries().filter(filesize=target.filesize).values_list("pk", flat=True)
        return self.gallery_candidates(target, ids, max_matches)


class LocalCrc32Matcher(LocalMatcher):
    """Other archives of the same file (same CRC32) that are already matched."""

    name = "crc32"
    type = "hash"

    def find(self, target: MatchTarget, cutoff: float, max_matches: int) -> list[MatchCandidate]:
        from viewer.models import Archive

        if not target.crc32:
            return []
        archives = Archive.objects.filter(crc32=target.crc32, gallery__isnull=False)
        if target.archive_id is not None:
            archives = archives.exclude(pk=target.archive_id)
        elif target.file_path:
            archives = archives.exclude(zipped=target.file_path)
        ids = archives.values_list("gallery_id", flat=True)
        return self.gallery_candidates(target, ids, max_matches)


class _ItemHashMatcher(LocalMatcher):
    """Galleries whose stored image hashes ("hash-compare" ItemProperties) equal the archive's."""

    def gallery_ids_with_hash(self, algorithm: str, value: str) -> list[int]:
        from viewer.models import Gallery, ItemProperties

        gallery_type = ContentType.objects.get_for_model(Gallery)
        return [
            int(object_id)
            for object_id in ItemProperties.objects.filter(
                content_type=gallery_type, tag="hash-compare", name=algorithm, value=value
            ).values_list("object_id", flat=True)
        ]


class LocalThumbnailHashMatcher(_ItemHashMatcher):
    name = "thumbnail"
    type = "hash_thumbnail"

    def find(self, target: MatchTarget, cutoff: float, max_matches: int) -> list[MatchCandidate]:
        from viewer.models import Archive, ItemProperties

        if target.archive_id is None:
            return []
        archive_type = ContentType.objects.get_for_model(Archive)
        candidates: list[MatchCandidate] = []
        for algorithm, value in ItemProperties.objects.filter(
            content_type=archive_type, object_id=target.archive_id, tag="hash-compare"
        ).values_list("name", "value"):
            candidates.extend(
                self.gallery_candidates(
                    target,
                    self.gallery_ids_with_hash(algorithm, value),
                    max_matches,
                    match_type="hash_thumbnail_{}".format(algorithm),
                )
            )
        return candidates


class LocalImageHashMatcher(_ItemHashMatcher):
    name = "image"
    type = "hash_image"

    def find(self, target: MatchTarget, cutoff: float, max_matches: int) -> list[MatchCandidate]:
        from viewer.models import Image, ItemProperties

        if target.archive_id is None:
            return []
        image_type = ContentType.objects.get_for_model(Image)
        positions = dict(Image.objects.filter(archive_id=target.archive_id).values_list("pk", "position"))
        if not positions:
            return []
        candidates: list[MatchCandidate] = []
        for object_id, algorithm, value in ItemProperties.objects.filter(
            content_type=image_type, object_id__in=list(positions), tag="hash-compare"
        ).values_list("object_id", "name", "value"):
            candidates.extend(
                self.gallery_candidates(
                    target,
                    self.gallery_ids_with_hash(algorithm, value),
                    max_matches,
                    match_type="hash_image_{}_{}".format(positions.get(int(object_id)), algorithm),
                )
            )
        return candidates


LOCAL_MATCHER_CLASSES: dict[str, type[LocalMatcher]] = {
    "crc32": LocalCrc32Matcher,
    "gid": LocalGidMatcher,
    "image": LocalImageHashMatcher,
    "thumbnail": LocalThumbnailHashMatcher,
    "title": LocalTitleMatcher,
    "size": LocalSizeMatcher,
}
