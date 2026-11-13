"""Runs matchers in online or offline mode and stores the results.

The engine is created once per run (a task, a folder crawl, a view action) and reused for every target, so local
gallery indexes are loaded once and providers that are banned are skipped for the rest of the run.

Online requests are paced by the database rate limiter inside each matcher, so there are no sleeps here.
"""

import logging
import os
import typing
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from core.base.cancellation import TaskCancelledError, ping_activity
from core.base.errors import ProviderBannedError
from core.base.utilities import get_zip_fileinfo
from core.matching.local import (
    LOCAL_MATCHER_CLASSES,
    LOCAL_METHODS,
    GalleryScope,
    LocalGidMatcher,
    LocalMatcher,
    LocalTitleMatcher,
)
from core.matching.types import MatchCandidate, MatchingMode, MatchTarget, dedupe_candidates

if typing.TYPE_CHECKING:
    from core.base.matchers import Matcher
    from core.base.setup import Settings
    from viewer.models import Archive, Gallery

logger = logging.getLogger(__name__)

AnyMatcher = typing.Union["Matcher", LocalMatcher]


@dataclass
class MatchingOptions:
    mode: MatchingMode = MatchingMode.ONLINE
    # Online: matcher name filters ("chaika", "panda_title"). Offline: gallery provider filters. Empty means all.
    filters: tuple[str, ...] = ()
    # Online: matcher types ("title", "hash", ...). Offline: local methods (see LOCAL_METHODS). Empty means all.
    methods: tuple[str, ...] = ()
    # Minimum title similarity for a candidate.
    cutoff: float = 0.4
    max_matches: int = 20
    # Link the target to the best candidate, instead of only storing the candidates as possible matches.
    auto_link: bool = False
    # Minimum accuracy to auto-link a title candidate. None uses each matcher's default.
    auto_link_cutoff: float | None = None
    # Offline: also apply clean_title to the file name before comparing.
    clean_title: bool = False
    # Offline: remove the previous possible matches of an archive before storing new ones.
    clear_previous: bool = False
    # Online matcher selection. With filters, every matching matcher is used unless enabled_only is set. Without
    # filters, only the matchers enabled in the settings are used, unless all_matchers is set.
    enabled_only: bool = False
    all_matchers: bool = False

    def __post_init__(self) -> None:
        self.mode = MatchingMode(self.mode)
        self.filters = tuple(x for x in self.filters if x)
        self.methods = tuple(x for x in self.methods if x and x != "all")


@dataclass
class MatchOutcome:
    candidates: list[MatchCandidate] = field(default_factory=list)
    best: MatchCandidate | None = None
    best_matcher: AnyMatcher | None = None


@dataclass
class MatchingReport:
    processed: int = 0
    with_candidates: int = 0
    linked: int = 0
    errors: int = 0
    # provider -> why it was skipped for the rest of the run.
    skipped_providers: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        text = "processed: {}, with candidates: {}, linked: {}, errors: {}".format(
            self.processed, self.with_candidates, self.linked, self.errors
        )
        if self.skipped_providers:
            text += ", skipped providers: " + "; ".join(
                "{} ({})".format(provider, reason) for provider, reason in self.skipped_providers.items()
            )
        return text


class MatchingEngine:
    def __init__(
        self,
        settings: "Settings",
        options: MatchingOptions,
        matchers: Sequence[AnyMatcher] | None = None,
    ) -> None:
        self.settings = settings
        self.options = options
        self.report = MatchingReport()
        self.matchers: list[AnyMatcher] = list(matchers) if matchers is not None else self._build_matchers()
        # Shared by the online matchers and cleared for each target: see Matcher.response_cache.
        self.response_cache: dict[tuple[typing.Any, ...], typing.Any] = {}
        for matcher in self.matchers:
            if not isinstance(matcher, LocalMatcher):
                matcher.response_cache = self.response_cache

    @property
    def online(self) -> bool:
        return self.options.mode == MatchingMode.ONLINE

    def __str__(self) -> str:
        return "MatchingEngine({}, matchers: {})".format(
            self.options.mode, ", ".join(str(x) for x in self.matchers) or "none"
        )

    # Matcher selection.

    def _build_matchers(self) -> list[AnyMatcher]:
        return self._online_matchers() if self.online else self._offline_matchers()

    def _provider_matchers(self, filter_name: str | None, force: bool) -> list["Matcher"]:
        return [
            x[0] for x in self.settings.provider_context.get_matchers(self.settings, filter_name=filter_name, force=force)
        ]

    def _online_matchers(self) -> list[AnyMatcher]:
        if self.options.filters:
            found: list["Matcher"] = []
            for filter_name in self.options.filters:
                found.extend(self._provider_matchers(filter_name, force=not self.options.enabled_only))
        else:
            found = self._provider_matchers(None, force=self.options.all_matchers)
        unique: dict[str, "Matcher"] = {}
        for matcher in found:
            if not self.options.methods or matcher.type in self.options.methods:
                unique.setdefault(str(matcher), matcher)
        return list(unique.values())

    def _formatter_for(self, provider: str, matcher_type: str) -> typing.Callable[[str], str] | None:
        """The provider's own title formatting, so files are compared the way that provider names them."""
        if not provider or provider not in self.settings.providers:
            return None
        for matcher in self._provider_matchers("{}_".format(provider), force=True):
            if matcher.provider == provider and matcher.type == matcher_type:
                return matcher.format_to_compare_title
        return None

    def _offline_matchers(self) -> list[AnyMatcher]:
        methods = [x for x in LOCAL_METHODS if not self.options.methods or x in self.options.methods]
        unknown = set(self.options.methods) - set(LOCAL_METHODS)
        if unknown:
            logger.warning("Unknown offline matching methods ignored: {}".format(", ".join(sorted(unknown))))
        matchers: list[AnyMatcher] = []
        for provider in self.options.filters or ("",):
            scope = GalleryScope((provider,))
            for method in methods:
                if method == "title":
                    matchers.append(
                        LocalTitleMatcher(
                            scope,
                            formatter=self._formatter_for(provider, "title"),
                            apply_clean_title=self.options.clean_title,
                        )
                    )
                elif method == "gid":
                    matchers.append(LocalGidMatcher(scope, formatter=self._formatter_for(provider, "gid")))
                else:
                    matchers.append(LOCAL_MATCHER_CLASSES[method](scope))
        return matchers

    # Running.

    def _run_matcher(self, matcher: AnyMatcher, target: MatchTarget) -> list[MatchCandidate]:
        provider = matcher.provider
        if provider in self.report.skipped_providers:
            return []
        ping_activity(getattr(self.settings, "activity_callback", None))
        try:
            if isinstance(matcher, LocalMatcher):
                return matcher.find(target, self.options.cutoff, self.options.max_matches)
            return matcher.find_online(target, cutoff=self.options.cutoff, max_matches=self.options.max_matches)
        except TaskCancelledError:
            raise
        except ProviderBannedError as e:
            reason = "banned or in cooldown, retry in {:.0f}s".format(e.retry_after)
            self.report.skipped_providers[provider] = reason
            logger.warning(
                "Matching: provider {} is {}. Skipping its matchers for the rest of this run.".format(provider, reason)
            )
            return []
        except Exception:
            self.report.errors += 1
            logger.exception("Matcher {} failed for target: {}".format(matcher, target.label))
            return []

    def process(self, target: MatchTarget) -> MatchOutcome:
        """Run the matchers on target.

        With auto_link, matchers run in priority order and stop at the first one with a good enough candidate.
        Otherwise every matcher runs, and all candidates are returned.
        """
        outcome = MatchOutcome()
        self.response_cache.clear()
        for matcher in self.matchers:
            candidates = self._run_matcher(matcher, target)
            outcome.candidates.extend(candidates)
            if self.options.auto_link and candidates:
                best = matcher.best_candidate(candidates, self.options.auto_link_cutoff)
                if best is not None:
                    outcome.best = best
                    outcome.best_matcher = matcher
                    break
        self.response_cache.clear()
        outcome.candidates = dedupe_candidates(outcome.candidates)
        self.report.processed += 1
        if outcome.candidates:
            self.report.with_candidates += 1
        return outcome

    # Storing results.

    def resolve_gallery(self, candidate: MatchCandidate, update_existing: bool = True) -> "Gallery | None":
        """The local Gallery for a candidate. Online candidates are created (or updated) from their fetched data."""
        from viewer.models import Gallery

        if candidate.gallery_id is not None:
            return Gallery.objects.filter(pk=candidate.gallery_id).first()
        gallery_data = candidate.gallery_data
        if gallery_data is None:
            return None
        if update_existing:
            return Gallery.objects.update_or_create_from_values(gallery_data)
        existing = Gallery.objects.filter(gid=gallery_data.gid, provider=gallery_data.provider).first()
        return existing or Gallery.objects.add_from_values(gallery_data)

    def save_possible_matches(self, archive: "Archive", candidates: Iterable[MatchCandidate]) -> int:
        from viewer.models import ArchiveMatches

        candidates = list(candidates)
        if self.options.clear_previous and candidates:
            archive.possible_matches.clear()
        saved = 0
        for candidate in candidates:
            gallery = self.resolve_gallery(candidate)
            if gallery is None:
                continue
            ArchiveMatches.objects.update_or_create(
                archive=archive,
                gallery=gallery,
                match_type=candidate.match_type,
                defaults={"match_accuracy": candidate.accuracy},
            )
            saved += 1
        return saved

    def link_archive(self, archive: "Archive", candidate: MatchCandidate) -> "Gallery | None":
        """Link an existing archive to the candidate's gallery (like selecting a possible match manually)."""
        gallery = self.resolve_gallery(candidate, update_existing=bool(self.settings.replace_metadata))
        if gallery is None:
            return None
        archive.gallery = gallery
        archive.set_titles_from_gallery(gallery, dont_save=True)
        archive.match_type = candidate.found_by
        archive.possible_matches.clear()
        archive.simple_save()
        archive.set_tags_from_gallery(gallery)
        archive.mark_if_different_size_from_gallery()
        if archive.public and not gallery.public:
            gallery.public = True
            gallery.save()
        self.report.linked += 1
        return gallery

    def link_file(self, file_path: str, crc32: str, candidate: MatchCandidate) -> "Archive | None":
        """Create or update the archive for a file (relative to MEDIA_ROOT), linked to the candidate's gallery."""
        from viewer.models import Archive

        gallery = self.resolve_gallery(candidate, update_existing=bool(self.settings.replace_metadata))
        if gallery is None:
            return None
        filesize, filecount, _ = get_zip_fileinfo(os.path.join(self.settings.MEDIA_ROOT, file_path))
        values: dict[str, typing.Any] = {
            "title": gallery.title or gallery.title_jpn or candidate.title,
            "title_jpn": gallery.title_jpn or "",
            "zipped": file_path,
            "crc32": crc32,
            "match_type": candidate.found_by,
            "filesize": filesize,
            "filecount": filecount,
            "source_type": gallery.provider,
        }
        if self.settings.archive_reason:
            values["reason"] = self.settings.archive_reason
        if self.settings.archive_details:
            values["details"] = self.settings.archive_details
        archive = Archive.objects.update_or_create_by_values_and_gid(
            values, (gallery.gid, gallery.provider), zipped=file_path
        )
        self.report.linked += 1
        return archive

    def match_archive(self, archive: "Archive") -> MatchOutcome:
        """Find candidates for an existing archive, and link it or store them as possible matches.

        Online title matchers search with the archive title. Offline ones compare the file name, which keeps the
        original naming (the archive title may already be a guess).
        """
        target = MatchTarget.from_archive(archive, use_title=self.online)
        if not target.file_path and not target.title:
            return MatchOutcome()
        outcome = self.process(target)
        if self.options.auto_link and outcome.best is not None:
            gallery = self.link_archive(archive, outcome.best)
            logger.info(
                "Archive {} linked to gallery {} (matcher: {}, accuracy: {:.2f}).".format(
                    archive.pk, gallery.get_link() if gallery else None, outcome.best.matcher, outcome.best.accuracy
                )
            )
        elif outcome.candidates:
            saved = self.save_possible_matches(archive, outcome.candidates)
            logger.info("Archive {}: stored {} possible matches.".format(archive.pk, saved))
        else:
            logger.info("Archive {}: no candidates found.".format(archive.pk))
        return outcome

    def match_archives(self, archives: Iterable["Archive"]) -> MatchingReport:
        logger.info("Matching archives with {}".format(self))
        for archive in archives:
            if archive.gallery_id:
                logger.info("Archive {} is already matched, skipping.".format(archive.pk))
                continue
            self.match_archive(archive)
        logger.info("Matching archives ended ({}): {}".format(self.options.mode, self.report.summary()))
        return self.report


def available_offline_methods() -> tuple[str, ...]:
    return LOCAL_METHODS
