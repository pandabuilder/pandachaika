import logging
import threading
import traceback
from typing import Any
from collections.abc import Callable, Iterable

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import QuerySet

from core.base.comparison import get_list_closer_text_from_list
from core.base.utilities import replace_illegal_name, get_title_from_path, clean_title as clean_title_text
from core.matching.engine import MatchingEngine, MatchingOptions, MatchingReport, available_offline_methods
from core.matching.local import GalleryScope, LocalTitleMatcher
from core.matching.types import MatchingMode, MatchTarget
from viewer.models import (
    GalleryMatch,
    Archive,
    Gallery,
    FoundGallery,
    ItemProperties,
    GalleryMatchGroup,
    GalleryGroupPossibleMatches
)

crawler_settings = settings.CRAWLER_SETTINGS

logger = logging.getLogger(__name__)


# WantedGallery
def _match_wanted_galleries(engine: MatchingEngine, wanted_galleries: Iterable[Any], label: str) -> MatchingReport:
    for wanted_gallery in wanted_galleries:
        if not wanted_gallery.search_title:
            continue
        outcome = engine.process(MatchTarget(title=wanted_gallery.search_title))
        for candidate in outcome.candidates:
            if candidate.gallery_id is not None:
                # Offline: the same gallery list is used for every wanted gallery, so found ones are filtered here.
                if FoundGallery.objects.filter(wanted_gallery=wanted_gallery, gallery_id=candidate.gallery_id).exists():
                    continue
            elif candidate.gallery_data is not None:
                candidate.gallery_data.dl_type = "gallery_match"
            gallery = engine.resolve_gallery(candidate)
            if gallery:
                GalleryMatch.objects.get_or_create(
                    wanted_gallery=wanted_gallery, gallery=gallery, defaults={"match_accuracy": candidate.accuracy}
                )
        logger.info(
            "Searched ({}) for wanted gallery: {}, candidates: {}, total possible matches: {}".format(
                label, wanted_gallery.title, len(outcome.candidates), wanted_gallery.possible_matches.count()
            )
        )
    logger.info("Wanted gallery matching ({}) ended: {}".format(label, engine.report.summary()))
    return engine.report


def create_matches_wanted_galleries_from_providers(
    wanted_galleries: QuerySet, provider: str, cutoff: float = 0.4, max_matches: int = 20
) -> None:
    """Online: search the providers' title matchers for each wanted gallery's search title."""
    try:
        engine = MatchingEngine(
            crawler_settings,
            MatchingOptions(
                mode=MatchingMode.ONLINE,
                filters=(provider,) if provider else (),
                methods=("title",),
                cutoff=cutoff,
                max_matches=max_matches,
                all_matchers=True,
            ),
        )
        _match_wanted_galleries(engine, wanted_galleries, "online")
    except BaseException:
        logger.critical(traceback.format_exc())


def create_matches_wanted_galleries_from_providers_internal(
    wanted_galleries: QuerySet,
    provider_filter: str = "",
    cutoff: float = 0.4,
    max_matches: int = 20,
    must_be_used: bool = False,
) -> None:
    """Offline: compare each wanted gallery's search title with the local gallery titles."""
    try:
        scope = GalleryScope((provider_filter,), must_be_used=must_be_used)
        matcher = LocalTitleMatcher(scope, formatter=lambda text: text, index_normalizer=clean_title_text)
        engine = MatchingEngine(
            crawler_settings,
            MatchingOptions(mode=MatchingMode.OFFLINE, filters=(provider_filter,), cutoff=cutoff, max_matches=max_matches),
            matchers=[matcher],
        )
        _match_wanted_galleries(engine, wanted_galleries, "offline")
    except BaseException:
        logger.critical(traceback.format_exc())


# Archive
def generate_possible_matches_for_archives(
    archives: "QuerySet[Archive] | None",
    cutoff: float = 0.4,
    max_matches: int = 20,
    filters: Iterable[str] = (),
    match_local: bool = True,
    match_web: bool = True,
    method_filter: str = "",
    methods: Iterable[str] = (),
    auto_link: bool = False,
    auto_link_cutoff: float | None = None,
    clean_title: bool = False,
    **_ignored: Any,
) -> None:
    """Match archives (non-matches if none given) offline (local galleries), online (provider searches), or both.

    filters are gallery provider filters offline, and matcher name filters online. methods restrict the offline
    methods / online matcher types. With auto_link, each archive is linked to
    its best candidate when one is good enough, otherwise the candidates are stored as possible matches.
    Offline runs first, so archives it links aren't searched online.
    """
    try:
        if not archives:
            archives = Archive.objects.filter(match_type="non-match")

        selected_methods = tuple(methods)
        if not selected_methods and method_filter == "gid":
            # Legacy option (tasks queued before the matching refactor): compare gids instead of titles, the other
            # methods always ran.
            selected_methods = tuple(x for x in available_offline_methods() if x != "title")

        modes = []
        if match_local:
            modes.append(MatchingMode.OFFLINE)
        if match_web:
            modes.append(MatchingMode.ONLINE)

        for mode in modes:
            engine = MatchingEngine(
                crawler_settings,
                MatchingOptions(
                    mode=mode,
                    filters=tuple(filters),
                    methods=selected_methods,
                    cutoff=cutoff,
                    max_matches=max_matches,
                    auto_link=auto_link,
                    auto_link_cutoff=auto_link_cutoff,
                    clean_title=clean_title,
                ),
            )
            # Re-evaluate the queryset each time, archives linked by a previous mode are skipped.
            engine.match_archives(archives.all() if hasattr(archives, "all") else archives)

        logger.info("Matching ended, local: {}, web {}".format(match_local, match_web))
    except BaseException:
        logger.critical(traceback.format_exc())


# GalleryMatchGroup
def generate_possible_matches_for_gallery_match_groups(
    gallery_match_groups: "QuerySet[GalleryMatchGroup]",
    cutoff: float = 0.4,
    max_matches: int = 20,
    providers: Iterable[str] = (),
    string_attributes_to_match: Iterable[str] = ("title", ),
) -> None:
    try:
        if gallery_match_groups:

            match_gallery_group_internal(
                gallery_match_groups,
                providers,
                cutoff=cutoff,
                max_matches=max_matches,
                match_by_filesize=True,
                string_attributes_to_match=string_attributes_to_match,
            )

        logger.info("Gallery Match Group matching ended")
        return
    except BaseException:
        logger.critical(traceback.format_exc())


def match_gallery_group_internal(
    gallery_match_groups: "QuerySet[GalleryMatchGroup]",
    providers: Iterable[str],
    cutoff: float = 0.4,
    max_matches: int = 20,
    match_by_filesize: bool = True,
    match_by_thumbnail: bool = True,
    string_attributes_to_match: Iterable[str] = ("title", ),
) -> None:

    galleries_per_provider: dict[str, QuerySet[Gallery]] = {}
    galleries_attribute_id_type_per_provider: dict[str, list[tuple[str, str, str]]] = {}

    if providers:
        for provider in providers:
            galleries_per_provider[provider] = Gallery.objects.eligible_for_use(provider__contains=provider)
    else:
        galleries_per_provider["all"] = Gallery.objects.eligible_for_use()

    for provider, galleries in galleries_per_provider.items():
        galleries_attribute_id_type_per_provider[provider] = list()
        for gallery in galleries:
            if gallery.title and "title" in string_attributes_to_match:
                galleries_attribute_id_type_per_provider[provider].append(
                    (replace_illegal_name(gallery.title), str(gallery.pk), "title")
                )
            if gallery.title_jpn and "title" in string_attributes_to_match:
                galleries_attribute_id_type_per_provider[provider].append(
                    (replace_illegal_name(gallery.title_jpn), str(gallery.pk), "title")
                )
            if gallery.comment and "comment" in string_attributes_to_match:
                galleries_attribute_id_type_per_provider[provider].append(
                    (replace_illegal_name(gallery.comment), str(gallery.pk), "comment")
                )

    gallery_type = ContentType.objects.get_for_model(Gallery)

    for i, gallery_match_group in enumerate(gallery_match_groups, start=1):
        first_gallery: Gallery | None = gallery_match_group.galleries.first()
        if not first_gallery:
            continue

        for provider, galleries_attribute_id_type in galleries_attribute_id_type_per_provider.items():

            for attribute_match in string_attributes_to_match:
                adj_attribute = get_title_from_path(getattr(first_gallery, attribute_match))

                if adj_attribute:
                    galleries_title_id = [(x[0], x[1]) for x in galleries_attribute_id_type if x[2] == attribute_match and x[1] != str(first_gallery.pk)]

                    similar_list_provider = get_list_closer_text_from_list(
                        adj_attribute, galleries_title_id, cutoff, max_matches
                    )

                    if similar_list_provider is not None:

                        for similar in similar_list_provider:
                            gallery = Gallery.objects.get(pk=similar[1])

                            GalleryGroupPossibleMatches.objects.update_or_create(
                                gallery_match_group=gallery_match_group, gallery=gallery, match_type=attribute_match, match_accuracy=similar[2]
                            )

                        logger.info(
                            "{} of {}: Found {} matches for gallery: {}, using provider filter: {}, method filter: {}".format(
                                i,
                                gallery_match_groups.count(),
                                len(similar_list_provider),
                                getattr(first_gallery, attribute_match),
                                provider,
                                attribute_match,
                            )
                        )

        if match_by_filesize and first_gallery.filesize is not None and first_gallery.filesize > 0:
            if providers:
                galleries_same_size = Gallery.objects.filter(filesize=first_gallery.filesize, provider__in=providers).exclude(pk=first_gallery.pk)
            else:
                galleries_same_size = Gallery.objects.filter(filesize=first_gallery.filesize).exclude(pk=first_gallery.pk)
            if galleries_same_size.exists():

                logger.info(
                    "{} of {}: Found {} matches from filesize for gallery: {}".format(
                        i, str(gallery_match_groups.count()), str(galleries_same_size.count()), first_gallery.title
                    )
                )
                for similar_gallery in galleries_same_size:
                    gallery = Gallery.objects.get(pk=similar_gallery.pk)

                    GalleryGroupPossibleMatches.objects.update_or_create(
                        gallery_match_group=gallery_match_group,
                        gallery=gallery,
                        match_type="size",
                        match_accuracy=1,
                    )

        if match_by_thumbnail and first_gallery.thumbnail:
            current_gallery_hashes = ItemProperties.objects.filter(
                content_type=gallery_type, object_id=first_gallery.pk, tag="hash-compare"
            )

            for hash_result in current_gallery_hashes:
                galleries_hashes = ItemProperties.objects.filter(
                    content_type=gallery_type, tag="hash-compare", name=hash_result.name, value=hash_result.value
                ).exclude(content_type=gallery_type, object_id=first_gallery.pk)

                if galleries_hashes.exists():
                    logger.info(
                        "{} of {}: Found {} matches from thumbnail hashes from algorithm: {} for gallery: {}".format(
                            i, str(gallery_match_groups.count()), str(galleries_hashes.count()), hash_result.name, first_gallery.title
                        )
                    )
                    for similar_item in galleries_hashes:
                        gallery_object = similar_item.content_object

                        if not gallery_object:
                            continue

                        if providers and gallery_object and gallery_object.provider not in providers:
                            continue

                        GalleryGroupPossibleMatches.objects.update_or_create(
                            gallery_match_group=gallery_match_group,
                            gallery=gallery_object,
                            match_type="hash_thumbnail_{}".format(hash_result.name),
                            match_accuracy=1,
                        )


class MatchingThread(threading.Thread):
    """Thread subclass that executes matching tasks locally or dispatches them to TaskQueue if in web process."""

    def __init__(
        self,
        name: str,
        target: Callable,
        args: Iterable = (),
        kwargs: dict[str, Any] | None = None,
        user: Any = None,
        settings: Any | None = None,
    ) -> None:
        super().__init__(name=name)
        self.target = target
        self.target_args = tuple(args)
        self.target_kwargs = kwargs or {}
        self.user = user
        self.settings = settings

    def start(self, force_local: bool = False) -> None:
        from django.conf import settings as django_settings
        c_settings = self.settings or getattr(django_settings, "CRAWLER_SETTINGS", None)
        if force_local:
            super().start()
        else:
            from workers.client import enqueue_matching_task
            first_arg = self.target_args[0] if self.target_args else None
            provider = self.target_args[1] if len(self.target_args) > 1 and isinstance(self.target_args[1], str) else None
            user_obj = self.user
            if not user_obj and c_settings and getattr(c_settings, "archive_user", None):
                user_obj = c_settings.archive_user
            enqueue_matching_task(
                task_type=self.name,
                args=first_arg,
                matching_kwargs=self.target_kwargs,
                provider=provider,
                user=user_obj,
            )
            logger.info("Enqueued matching task '%s' to worker process", self.name)

    def run(self) -> None:
        try:
            self.target(*self.target_args, **self.target_kwargs)
        except BaseException:
            logger.critical(traceback.format_exc())

