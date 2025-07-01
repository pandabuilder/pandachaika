import logging
import traceback

import time
from collections.abc import Iterable

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import QuerySet, Q

from core.base.comparison import get_list_closer_text_from_list
from core.base.utilities import replace_illegal_name, get_title_from_path, clean_title
from viewer.models import (
    GalleryMatch,
    Archive,
    Gallery,
    ArchiveMatches,
    FoundGallery,
    ArchiveQuerySet,
    ItemProperties,
    Image,
    GalleryMatchGroup,
    GalleryGroupPossibleMatches
)

crawler_settings = settings.CRAWLER_SETTINGS

logger = logging.getLogger(__name__)


# WantedGallery
def create_matches_wanted_galleries_from_providers(
    wanted_galleries: QuerySet, provider: str, cutoff: float = 0.4, max_matches: int = 20
) -> None:

    try:
        matchers = crawler_settings.provider_context.get_matchers(
            crawler_settings, filter_name=provider, force=True, matcher_type="title"
        )
        for matcher_element in matchers:
            matcher = matcher_element[0]
            for wanted_gallery in wanted_galleries:
                results = matcher.create_closer_matches_values(
                    wanted_gallery.search_title, cutoff=cutoff, max_matches=max_matches
                )
                for gallery_data in results:
                    gallery_data[1].dl_type = "gallery_match"
                    gallery = Gallery.objects.update_or_create_from_values(gallery_data[1])
                    if gallery:
                        GalleryMatch.objects.get_or_create(
                            wanted_gallery=wanted_gallery, gallery=gallery, defaults={"match_accuracy": gallery_data[2]}
                        )
                if results:
                    logger.info(
                        "Searched for wanted gallery: {} in panda, total possible matches: {}".format(
                            wanted_gallery.title, wanted_gallery.possible_matches.count()
                        )
                    )
                else:
                    logger.info("Searched for wanted gallery: {} in panda, no match found".format(wanted_gallery.title))
                # same wait_timer for all providers
                time.sleep(crawler_settings.wait_timer)
        logger.info("Search ended.")
    except BaseException:
        logger.critical(traceback.format_exc())


# WantedGallery
def create_matches_wanted_galleries_from_providers_internal(
    wanted_galleries: QuerySet,
    provider_filter: str = "",
    cutoff: float = 0.4,
    max_matches: int = 20,
    must_be_used: bool = False,
) -> None:

    try:
        galleries_title_id = []

        if provider_filter:
            galleries = Gallery.objects.eligible_for_use(provider__contains=provider_filter)
        else:
            galleries = Gallery.objects.eligible_for_use()

        if must_be_used:
            galleries = galleries.filter(
                Q(archive__isnull=False)
                | (Q(gallery_container__isnull=False) & Q(gallery_container__archive__isnull=False))
                | (Q(magazine__isnull=False) & Q(magazine__archive__isnull=False))
            )

        for gallery in galleries:
            if gallery.title:
                galleries_title_id.append((clean_title(gallery.title), gallery.pk))
            if gallery.title_jpn:
                galleries_title_id.append((clean_title(gallery.title_jpn), gallery.pk))

        logger.info(
            "Trying to match against gallery database, "
            "{} wanted galleries with no match. Provider filter: {}".format(wanted_galleries.count(), provider_filter)
        )
        for wanted_gallery in wanted_galleries:

            similar_list = get_list_closer_text_from_list(
                wanted_gallery.search_title, galleries_title_id, cutoff, max_matches
            )

            if similar_list is not None:

                logger.info("Found {} matches from title for {}".format(len(similar_list), wanted_gallery.search_title))
                for similar in similar_list:
                    # We filter here instead of the Gallery model, because we use the same list for every WG.
                    if not FoundGallery.objects.filter(wanted_gallery=wanted_gallery, gallery_id=similar[1]):
                        GalleryMatch.objects.get_or_create(
                            wanted_gallery=wanted_gallery,
                            gallery_id=similar[1],
                            defaults={"match_accuracy": similar[2]},
                        )

        logger.info("Matching ended")
        return
    except BaseException:
        logger.critical(traceback.format_exc())


# Archive
def search_for_archives_matches_web(archives: ArchiveQuerySet, matcher_filter: str = "") -> None:

    try:
        logger.info('Matching using match filter "{}" started for {} archives.'.format(matcher_filter, len(archives)))
        if matcher_filter:
            matchers = crawler_settings.provider_context.get_matchers(
                crawler_settings, filter_name=matcher_filter, force=True
            )
        else:
            matchers = crawler_settings.provider_context.get_matchers(crawler_settings, force=False)
        for matcher_element in matchers:
            matcher = matcher_element[0]
            for archive_obj in archives:
                if archive_obj.gallery:
                    logger.info("Archive {} is already matched, skipping.".format(archive_obj.title))
                    continue
                if matcher.type == "title":
                    results = matcher.create_closer_matches_values(archive_obj.title, cutoff=0.4)
                elif matcher.type == "image":
                    results = matcher.create_closer_matches_values(archive_obj.zipped.name)
                else:
                    results = matcher.create_closer_matches_values(archive_obj.zipped.name)
                for result in results:
                    gallery = Gallery.objects.update_or_create_from_values(result[1])
                    ArchiveMatches.objects.update_or_create(
                        archive=archive_obj,
                        gallery=gallery,
                        match_type=matcher.type,
                        defaults={
                            "match_accuracy": result[2],
                        },
                    )
                if results:
                    logger.info(
                        "Searched for archive with title: {} with matcher: {}, "
                        "found results: {} of processed search results: {}. "
                        "Total search results: {}".format(
                            archive_obj.title,
                            matcher,
                            archive_obj.possible_matches.count(),
                            len(matcher.values_array),
                            len(matcher.gallery_links),
                        )
                    )
                else:
                    logger.info(
                        "Searched for archive with title: {} with matcher: {}, "
                        "no match found in processed search results: {}. "
                        "Total search results: {}".format(
                            archive_obj.title, matcher, len(matcher.values_array), len(matcher.gallery_links)
                        )
                    )
                # same wait_timer for all providers
                time.sleep(crawler_settings.wait_timer)
        logger.info("Search ended.")
    except BaseException:
        logger.critical(traceback.format_exc())


# Archive
def match_archives_from_gallery_titles(
    archives: "QuerySet[Archive]", cutoff: float = 0.4, max_matches: int = 20, provider: str = ""
) -> None:

    try:
        if not archives:
            non_match_archives: "QuerySet[Archive]" = Archive.objects.filter(match_type="non-match")
        else:
            non_match_archives = archives

        if non_match_archives:

            galleries_title_id = []

            if provider:
                galleries = Gallery.objects.eligible_for_use(provider__contains=provider)
            else:
                galleries = Gallery.objects.eligible_for_use()
            for gallery in galleries:
                if gallery.title:
                    galleries_title_id.append((replace_illegal_name(gallery.title), gallery.pk))
                if gallery.title_jpn:
                    galleries_title_id.append((replace_illegal_name(gallery.title_jpn), gallery.pk))

            logger.info(
                "Trying to match against gallery database, "
                "{} archives with no match, matching against: {}, "
                "number of galleries: {}, cutoff: {}".format(
                    non_match_archives.count(), provider, galleries.count(), cutoff
                )
            )
            for i, archive in enumerate(non_match_archives, start=1):

                matchers = crawler_settings.provider_context.get_matchers(
                    crawler_settings, filter_name="{}_title".format(provider), force=True
                )

                if matchers:
                    adj_title = matchers[0][0].format_to_compare_title(archive.zipped.name)
                else:
                    adj_title = get_title_from_path(archive.zipped.name)
                similar_list = get_list_closer_text_from_list(
                    adj_title, galleries_title_id, cutoff, max_matches
                )

                if similar_list is not None:

                    archive.possible_matches.clear()

                    logger.info(
                        "{} of {}: Found {} matches from title for {}".format(
                            i, non_match_archives.count(), len(similar_list), archive.zipped.name
                        )
                    )
                    for similar in similar_list:
                        gallery = Gallery.objects.get(pk=similar[1])

                        ArchiveMatches.objects.create(
                            archive=archive, gallery=gallery, match_type="title", match_accuracy=similar[2]
                        )

                if archive.filesize is None or archive.filesize <= 0:
                    continue
                galleries_same_size = Gallery.objects.filter(filesize=archive.filesize)
                if galleries_same_size.exists():

                    logger.info(
                        "{} of {}: Found {} matches from filesize for {}".format(
                            i, str(non_match_archives.count()), str(galleries_same_size.count()), archive.zipped.name
                        )
                    )
                    for similar_gallery in galleries_same_size:
                        gallery = Gallery.objects.get(pk=similar_gallery.pk)

                        ArchiveMatches.objects.create(
                            archive=archive, gallery=gallery, match_type="size", match_accuracy=1
                        )

        logger.info("Matching ended")
        return
    except BaseException:
        logger.critical(traceback.format_exc())


# Archive
def generate_possible_matches_for_archives(
    archives: "QuerySet[Archive]",
    cutoff: float = 0.4,
    max_matches: int = 20,
    filters: Iterable[str] = (),
    match_local: bool = True,
    match_web: bool = True,
    update_if_local: bool = False,
    method_filter: str = "title",
) -> None:
    # TODO: Not implemented: update_if_local, expose match_by_filesize.
    try:
        if not archives:
            non_match_archives: "QuerySet[Archive]" = Archive.objects.filter(match_type="non-match")
        else:
            non_match_archives = archives

        if non_match_archives:

            if match_local:
                match_internal(
                    non_match_archives,
                    filters,
                    cutoff=cutoff,
                    max_matches=max_matches,
                    match_by_filesize=True,
                    method_filter=method_filter,
                )
            if match_web:
                match_external(non_match_archives, filters, cutoff=cutoff, max_matches=max_matches)

        logger.info("Matching ended, local: {}, web {}".format(match_local, match_web))
        return
    except BaseException:
        logger.critical(traceback.format_exc())


def match_external(
    archives: "QuerySet[Archive]", matcher_filters: Iterable[str], cutoff: float = 0.4, max_matches: int = 20
) -> None:
    matchers_per_matcher_filter = {}
    if matcher_filters:
        for matcher_filter in matcher_filters:
            matchers = crawler_settings.provider_context.get_matchers(
                crawler_settings, filter_name=matcher_filter, force=True
            )
            matchers_per_matcher_filter[matcher_filter] = matchers

    else:
        matchers_per_matcher_filter["all"] = crawler_settings.provider_context.get_matchers(
            crawler_settings, force=False
        )

    for matcher_filter, matchers in matchers_per_matcher_filter.items():
        for matcher in matchers:
            logger.info("For filter {}, using matcher: {}".format(matcher_filter, str(matcher[0])))

    for i, archive in enumerate(archives, start=1):

        for matcher_filter, matchers in matchers_per_matcher_filter.items():

            for matcher in matchers:

                if matcher[0].type == "title":
                    if archive.title:
                        results = matcher[0].create_closer_matches_values(
                            archive.title, cutoff=cutoff, max_matches=max_matches
                        )
                    else:
                        results = []
                elif matcher[0].type == "image":
                    results = matcher[0].create_closer_matches_values(archive.zipped.name)
                else:
                    results = matcher[0].create_closer_matches_values(archive.zipped.name)
                for result in results:
                    gallery = Gallery.objects.update_or_create_from_values(result[1])
                    ArchiveMatches.objects.update_or_create(
                        archive=archive,
                        gallery=gallery,
                        match_type=matcher[0].type,
                        defaults={
                            "match_accuracy": result[2],
                        },
                    )
                if results:
                    logger.info(
                        "{} of {}: Found {} matches (external search) for archive: {} using matcher: {}, "
                        "Processed search results: {}. "
                        "Total search results: {}".format(
                            i,
                            archives.count(),
                            len(results),
                            archive.title,
                            matcher[0],
                            len(matcher[0].values_array),
                            len(matcher[0].gallery_links),
                        )
                    )
                else:
                    logger.info(
                        "{} of {}: Found no matches (external search) for archive: {} using matcher: {}.".format(
                            i, archives.count(), archive.title, matcher[0]
                        )
                    )


def match_internal(
    archives: "QuerySet[Archive]",
    providers: Iterable[str],
    cutoff: float = 0.4,
    max_matches: int = 20,
    match_by_filesize: bool = True,
    match_by_thumbnail: bool = True,
    method_filter: str = "title",
) -> None:

    galleries_per_provider: dict[str, QuerySet[Gallery]] = {}
    galleries_title_id_type_per_provider: dict[str, list[tuple[str, str, str]]] = {}

    if providers:
        for provider in providers:
            galleries_per_provider[provider] = Gallery.objects.eligible_for_use(provider__contains=provider)
    else:
        galleries_per_provider["all"] = Gallery.objects.eligible_for_use()

    for provider, galleries in galleries_per_provider.items():
        galleries_title_id_type_per_provider[provider] = list()
        for gallery in galleries:
            if gallery.title:
                galleries_title_id_type_per_provider[provider].append(
                    (replace_illegal_name(gallery.title), str(gallery.pk), "title")
                )
            if gallery.title_jpn:
                galleries_title_id_type_per_provider[provider].append(
                    (replace_illegal_name(gallery.title_jpn), str(gallery.pk), "title")
                )
            if gallery.gid:
                galleries_title_id_type_per_provider[provider].append((gallery.gid, str(gallery.pk), "gid"))

    image_type = ContentType.objects.get_for_model(Image)
    archive_type = ContentType.objects.get_for_model(Archive)
    gallery_type = ContentType.objects.get_for_model(Gallery)

    for i, archive in enumerate(archives, start=1):

        for provider, galleries_title_id_type in galleries_title_id_type_per_provider.items():

            if provider != "all":
                matchers = crawler_settings.provider_context.get_matchers(
                    crawler_settings, filter_name="{}_{}".format(provider, method_filter), force=True
                )
                if matchers:
                    adj_title = matchers[0][0].format_to_compare_title(archive.zipped.name)
                    attribute_match = method_filter
                else:
                    adj_title = get_title_from_path(archive.zipped.name)
                    attribute_match = "title"
            else:
                adj_title = get_title_from_path(archive.zipped.name)
                attribute_match = "title"

            if adj_title:
                galleries_title_id = [(x[0], x[1]) for x in galleries_title_id_type if x[2] == attribute_match]

                similar_list_provider = get_list_closer_text_from_list(
                    adj_title, galleries_title_id, cutoff, max_matches
                )

                if similar_list_provider is not None:

                    for similar in similar_list_provider:
                        gallery = Gallery.objects.get(pk=similar[1])

                        ArchiveMatches.objects.update_or_create(
                            archive=archive, gallery=gallery, match_type=method_filter, match_accuracy=similar[2]
                        )

                    logger.info(
                        "{} of {}: Found {} matches (internal search) from title for archive: {}, with formatted title: ({}), using provider filter: {}, method filter: {}".format(
                            i,
                            archives.count(),
                            len(similar_list_provider),
                            archive.title,
                            adj_title,
                            provider,
                            method_filter,
                        )
                    )

        if not match_by_filesize or archive.filesize is None or archive.filesize <= 0:
            continue
        if providers:
            galleries_same_size = Gallery.objects.filter(filesize=archive.filesize, provider__in=providers)
        else:
            galleries_same_size = Gallery.objects.filter(filesize=archive.filesize)
        if galleries_same_size.exists():

            logger.info(
                "{} of {}: Found {} matches (internal search) from filesize for archive: {}".format(
                    i, str(archives.count()), str(galleries_same_size.count()), archive.title
                )
            )
            for similar_gallery in galleries_same_size:
                gallery = Gallery.objects.get(pk=similar_gallery.pk)

                ArchiveMatches.objects.update_or_create(
                    archive=archive, gallery=gallery, match_type="size", match_accuracy=1
                )

        if not match_by_thumbnail or not archive.thumbnail:
            continue
        current_archive_hashes = ItemProperties.objects.filter(
            content_type=archive_type, object_id=archive.pk, tag="hash-compare"
        )

        for hash_result in current_archive_hashes:
            galleries_hashes = ItemProperties.objects.filter(
                content_type=gallery_type, tag="hash-compare", name=hash_result.name, value=hash_result.value
            )

            if galleries_hashes.exists():
                logger.info(
                    "{} of {}: Found {} matches (internal search) from thumbnail hashes from algorithm: {} for archive: {}".format(
                        i, str(archives.count()), str(galleries_hashes.count()), hash_result.name, archive.title
                    )
                )
                for similar_item in galleries_hashes:
                    gallery_object = similar_item.content_object

                    if not gallery_object:
                        continue

                    if providers and gallery_object and gallery_object.provider not in providers:
                        continue

                    ArchiveMatches.objects.update_or_create(
                        archive=archive,
                        gallery=gallery_object,
                        match_type="hash_thumbnail_{}".format(hash_result.name),
                        match_accuracy=1,
                    )

        images = Image.objects.filter(archive=archive)

        current_images_hashes = ItemProperties.objects.filter(
            content_type=image_type, object_id__in=images, tag="hash-compare"
        )

        for hash_result in current_images_hashes:
            galleries_hashes = ItemProperties.objects.filter(
                content_type=gallery_type, tag="hash-compare", name=hash_result.name, value=hash_result.value
            )

            image = hash_result.content_object

            if image and galleries_hashes.exists():
                logger.info(
                    "{} of {}: Found {} matches (internal search) from image hashes from algorithm: {} for archive: {}, image: {}".format(
                        i,
                        str(archives.count()),
                        str(galleries_hashes.count()),
                        hash_result.name,
                        archive.title,
                        image.position,
                    )
                )
                for similar_item in galleries_hashes:

                    gallery_object = similar_item.content_object

                    # For some reason we are getting null violations when not doing this filtering.
                    # Maybe gallery gets deleted?
                    if not gallery_object:
                        continue

                    if providers and gallery_object and gallery_object.provider not in providers:
                        continue

                    ArchiveMatches.objects.update_or_create(
                        archive=archive,
                        gallery=gallery_object,
                        match_type="hash_image_{}_{}".format(image.position, hash_result.name),
                        match_accuracy=1,
                    )


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
