import enum
import typing
from dataclasses import dataclass, field

from core.base.types import GalleryData

if typing.TYPE_CHECKING:
    from viewer.models import Archive


class MatchingMode(enum.StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"


@dataclass(frozen=True)
class MatchTarget:
    """What is being matched.

    ``file_path`` is relative to MEDIA_ROOT. ``title`` is used by title based matchers instead of the file name when
    it's set (an Archive title, or a WantedGallery search title, which has no file at all).
    """

    file_path: str = ""
    title: str = ""
    crc32: str = ""
    filesize: int | None = None
    filecount: int | None = None
    archive_id: int | None = None

    @classmethod
    def from_archive(cls, archive: "Archive", use_title: bool = True) -> "MatchTarget":
        return cls(
            file_path=archive.zipped.name or "",
            title=(archive.title or "") if use_title else "",
            crc32=archive.crc32 or "",
            filesize=archive.filesize,
            filecount=archive.filecount,
            archive_id=archive.pk,
        )

    @property
    def text(self) -> str:
        """Text that title based matchers work from: the title if set, otherwise the file path."""
        return self.title or self.file_path

    @property
    def label(self) -> str:
        return self.title or self.file_path


@dataclass
class SearchResult:
    """Outcome of one online search.

    Some providers return the full metadata in the search response (``galleries``). The others only return links,
    and the metadata is fetched afterwards with the provider's parser.
    """

    links: list[str] = field(default_factory=list)
    galleries: list[GalleryData] | None = None


@dataclass
class MatchCandidate:
    """A gallery that may correspond to the target.

    Online candidates carry the fetched ``gallery_data`` (the Gallery may not exist locally yet). Offline candidates
    carry the local ``gallery_id``.
    """

    matcher: str
    match_type: str
    found_by: str
    accuracy: float
    title: str = ""
    gallery_data: GalleryData | None = None
    gallery_id: int | None = None
    # Exact matchers (hash, size, image) give every result accuracy 1. This similarity between the target and the
    # gallery titles picks the best one among them.
    title_similarity: float = 0.0

    @property
    def key(self) -> tuple[str, str]:
        if self.gallery_id is not None:
            return "pk", str(self.gallery_id)
        if self.gallery_data is not None:
            return self.gallery_data.provider or "", self.gallery_data.gid
        return "", self.title

    @property
    def link(self) -> str:
        if self.gallery_data is not None and self.gallery_data.link:
            return self.gallery_data.link
        return ""


def dedupe_candidates(candidates: typing.Iterable[MatchCandidate]) -> list[MatchCandidate]:
    """Keep one candidate per gallery and match type (the most accurate one), sorted by accuracy."""
    best: dict[tuple[str, tuple[str, str]], MatchCandidate] = {}
    for candidate in candidates:
        key = (candidate.match_type, candidate.key)
        current = best.get(key)
        if current is None or (candidate.accuracy, candidate.title_similarity) > (
            current.accuracy,
            current.title_similarity,
        ):
            best[key] = candidate
    return sorted(best.values(), key=lambda c: (c.accuracy, c.title_similarity), reverse=True)


def pick_best(candidates: list[MatchCandidate], exact: bool, cutoff: float) -> MatchCandidate | None:
    """Exact matchers: the result whose title is closest. Others: the most accurate one, if above cutoff."""
    if not candidates:
        return None
    if exact:
        return max(candidates, key=lambda c: c.title_similarity)
    best = max(candidates, key=lambda c: c.accuracy)
    return best if best.accuracy >= cutoff else None
