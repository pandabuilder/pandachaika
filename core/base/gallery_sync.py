import logging
import typing

from core.base.types import GalleryData
from viewer.models import ArchiveManageEntry, Gallery, WantedGallery

if typing.TYPE_CHECKING:
    from core.base.setup import Settings
    from core.base.setup_utilities import GeneralUtils

logger = logging.getLogger(__name__)


def sync_gallery_db_entry(
    settings: "Settings",
    general_utils: "GeneralUtils",
    gallery_data: GalleryData,
    downloader_type: str = "",
    no_metadata: bool = False,
    update_existing: bool | None = None,
) -> "Gallery | None":
    """Create or update the Gallery row from fetched metadata and propagate it to the related Archives.

    This is shared by the download flow (``BaseDownloader.update_gallery_db``) and the metadata updater
    (``core.web.gallery_updater``). It does no network requests.

    :param downloader_type: the type of the downloader doing the call (e.g. ``submit`` marks the origin).
    :param no_metadata: mark the Gallery as having no metadata.
    :param update_existing: if the call is refreshing an already known Gallery. In that case, newly banned data
        and invalidated WantedGallery matches mark the Archives for review. Defaults to the
        ``update_metadata_mode`` setting.
    """
    if update_existing is None:
        update_existing = bool(settings.update_metadata_mode)

    if settings.keep_dl_type and gallery_data.dl_type is not None:
        gallery_data.dl_type = None
    if downloader_type == "submit":
        gallery_data.origin = Gallery.OriginChoices.ORIGIN_SUBMITTED
    if no_metadata:
        gallery_data.status = Gallery.StatusChoices.NO_METADATA
    if settings.gallery_reason:
        gallery_data.reason = settings.gallery_reason

    gallery_db_entry = Gallery.objects.update_or_create_from_values(gallery_data)
    if not gallery_db_entry:
        return None

    gallery_db_entry.schedule_index_update()

    if settings.link_child is not None:
        child_gallery = Gallery.objects.filter(gid=settings.link_child, provider=gallery_db_entry.provider).first()
        if child_gallery:
            child_gallery.parent_gallery = gallery_db_entry
            child_gallery.save()

    if settings.link_newer is not None:
        newer_gallery = Gallery.objects.filter(gid=settings.link_newer, provider=gallery_db_entry.provider).first()
        if newer_gallery:
            newer_gallery.first_gallery = gallery_db_entry
            newer_gallery.save()

    # If we are updating a gallery's metadata, and we get a new tag that would have been rejected,
    # mark the archive. Do the same for a wanted match.
    wanted_invalidated: list["WantedGallery"] = []
    banned_result: bool = False
    banned_reasons: list[str] = []
    if update_existing:
        banned_result, banned_reasons = general_utils.discard_by_gallery_data(
            gallery_db_entry.tag_list(), force_check=True
        )
        if settings.recheck_wanted_on_update:
            wanted_found = WantedGallery.objects.filter(foundgallery__gallery=gallery_db_entry)

            rematch_wanted = gallery_db_entry.match_against_wanted_galleries(
                wanted_filters=wanted_found, skip_already_found=False
            )

            for single_wanted_found in wanted_found:
                if single_wanted_found not in rematch_wanted:
                    wanted_invalidated.append(single_wanted_found)

    for archive in gallery_db_entry.archive_set.all():
        if not archive.gallery:
            continue

        archive.set_titles_from_gallery(archive.gallery)

        if settings.recheck_wanted_on_update:
            for single_wanted_invalidated in wanted_invalidated:
                mark_comment = (
                    "The refreshed Gallery metadata invalidates the previously accepted WantedGallery: (special-link):({})({})"
                ).format(single_wanted_invalidated.title, single_wanted_invalidated.get_absolute_url())

                if not ArchiveManageEntry.objects.filter(
                    archive=archive, mark_reason="invalidated_wanted", mark_comment=mark_comment
                ).exists():
                    ArchiveManageEntry.objects.update_or_create(
                        archive=archive,
                        mark_reason="invalidated_wanted",
                        mark_comment=mark_comment,
                        defaults={
                            "mark_priority": 5.0,
                            "mark_check": True,
                            "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                        },
                    )

        archive_banned_result: bool = False

        if banned_result:
            archive_banned_result, _ = general_utils.discard_by_gallery_data(archive.tag_list(), force_check=True)

        archive.set_tags_from_gallery(archive.gallery)

        if banned_result and not archive_banned_result:
            mark_comment = "The refreshed Gallery has added banned data:\n{}".format(banned_reasons)

            ArchiveManageEntry.objects.update_or_create(
                archive=archive,
                mark_reason="banned_data",
                defaults={
                    "mark_comment": mark_comment,
                    "mark_priority": 5.0,
                    "mark_check": True,
                    "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                },
            )

    return gallery_db_entry
