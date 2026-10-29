from collections import Counter
import logging
import os
from os.path import join as pjoin
import re
import typing
import zipfile
import zlib

from django.conf import settings

from core.base.utilities import get_images_from_zip, get_zip_fileinfo

if typing.TYPE_CHECKING:
    from viewer.models import Archive

logger = logging.getLogger("viewer.models.archive")


class ArchiveIngestionService:
    """Service handling zip file ingestion, metadata synchronization, and initial processing for archives."""

    @classmethod
    def sync_gallery_metadata(cls, archive: "Archive") -> None:
        """Synchronize archive titles, tags, and parent-gallery marks from its linked gallery."""
        from viewer.models.archive import ArchiveOption

        if archive.pk is None:
            return

        archive_option = ArchiveOption.objects.filter(archive=archive).first()

        # title
        if archive.gallery and archive.gallery.title and (not archive_option or not archive_option.freeze_titles):
            archive.title = archive.gallery.title
            archive.possible_matches.clear()
        elif archive.title is None and archive.zipped and archive.zipped.name:
            archive.title = re.sub("[_]", " ", os.path.splitext(os.path.basename(archive.zipped.name))[0])

        # tags
        if archive.gallery and archive.gallery.tags.all():
            archive.set_tags_from_gallery(archive.gallery)

        # title_jpn
        if archive.gallery and archive.gallery.title_jpn and (not archive_option or not archive_option.freeze_titles):
            archive.title_jpn = archive.gallery.title_jpn

        archive.create_mark_if_parent_gallery()

    @classmethod
    def ingest(cls, archive: "Archive", save: bool = True) -> bool:
        """Ingest an archive's zip file: verify integrity, create image set, compute hashes/stats, and generate cover."""
        from viewer.models.archive import Archive, ArchiveManageEntry, Image, thumb_path_handler
        from viewer.services.archive_hashing import ArchiveHashingService
        from viewer.services.archive_thumbnail import ArchiveThumbnailService

        if archive.pk is None:
            super(Archive, archive).save()

        if not archive.zipped or not archive.zipped.name or not os.path.isfile(archive.zipped.path):
            cls.sync_gallery_metadata(archive)
            if save:
                archive.save(force_update=True)
            return False

        # crc32: Calculate CRC32 first, since it doesn't depend on the zipfile being correct or not.
        if archive.crc32 is None or archive.crc32 == "":
            archive.crc32 = ArchiveHashingService.calculate_archive_crc32(archive, use_pool=True)

        image_set_present = archive.image_set.exists()
        # large thumbnail and image set
        if not archive.thumbnail or not image_set_present:
            my_zip = None
            try:
                my_zip = zipfile.ZipFile(archive.zipped.path, "r")
                bad_file = my_zip.testzip()
                if bad_file:
                    logger.error(
                        "Zip file CRC check failed on file %s for archive: %s on file: %s",
                        bad_file,
                        archive,
                        archive.zipped.path,
                    )
                    my_zip.close()
                    if save:
                        archive.save(force_update=True)
                    return False
            except (zipfile.BadZipFile, NotImplementedError, zlib.error, OSError) as e:
                logger.error(
                    "Error opening or testing zipfile for archive: %s on file: %s: %s",
                    archive,
                    archive.zipped.path,
                    e,
                )
                if my_zip:
                    my_zip.close()
                if save:
                    archive.save(force_update=True)
                return False

            filtered_files = get_images_from_zip(my_zip)

            nested_images = [x for x in filtered_files if x[1] is not None]

            if len(nested_images) > 0:
                c = Counter(nested_image[1] for nested_image in nested_images)
                mark_comment_list = ["File: {} has: {} images".format(x, c[x]) for x in c.keys()]
                mark_comment = "\n".join(mark_comment_list)
                ArchiveManageEntry.objects.update_or_create(
                    archive=archive,
                    mark_reason="found_nested_files",
                    defaults={
                        "mark_comment": mark_comment,
                        "mark_priority": 1.0,
                        "mark_check": True,
                        "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                    },
                )

            if not image_set_present and filtered_files:
                Image.objects.bulk_create(
                    [
                        Image(archive=archive, archive_position=count, position=count, image=None)
                        for count, _ in enumerate(filtered_files, start=1)
                    ]
                )

            should_hash_images = bool(settings.CRAWLER_SETTINGS.auto_hash_images and not archive.thumbnail)

            if not archive.thumbnail and filtered_files:
                if image_set_present:
                    first_img = archive.image_set.first()
                    first_file = (
                        filtered_files[first_img.archive_position - 1]
                        if first_img and 0 < first_img.archive_position <= len(filtered_files)
                        else filtered_files[0]
                    )
                else:
                    first_file = filtered_files[0]

                thumb_name = thumb_path_handler(archive, "thumb2.jpg")
                output_path = pjoin(settings.MEDIA_ROOT, thumb_name)
                size = None
                try:
                    from core.base.process_pool import generate_thumbnail_in_pool

                    size = generate_thumbnail_in_pool(
                        archive.zipped.path, first_file[0], first_file[1], output_path
                    )
                except Exception:
                    size = None

                if size:
                    archive.thumbnail = thumb_name
                    archive.thumbnail_width, archive.thumbnail_height = size
                else:
                    if first_file[1] is None:
                        with my_zip.open(first_file[0]) as current_img:
                            ArchiveThumbnailService.create_thumbnail_from_io_image(archive, current_img)
                    else:
                        with my_zip.open(first_file[1]) as current_zip:
                            with zipfile.ZipFile(current_zip) as my_nested_zip:
                                with my_nested_zip.open(first_file[0]) as current_img:
                                    ArchiveThumbnailService.create_thumbnail_from_io_image(archive, current_img)
                if settings.CRAWLER_SETTINGS.auto_phash_images:
                    archive.create_or_update_thumbnail_hash("phash")

            my_zip.close()

            if should_hash_images:
                ArchiveHashingService.calculate_sha1_and_data_for_images(
                    archive,
                    process_image_data=True,
                    process_other_data=False,
                    process_archive_statistics=True,
                )

        cls.sync_gallery_metadata(archive)

        # size
        if archive.filesize is None or archive.filecount is None:
            try:
                archive.filesize, archive.filecount, other_file_datas = get_zip_fileinfo(
                    archive.zipped.path, get_extra_data=True
                )
                archive.fill_other_file_data(other_file_datas)
            except OSError as e:
                logger.error("Error reading zip fileinfo for archive %s: %s", archive, e)

        # original_filename
        if (archive.original_filename is None or archive.original_filename == "") and archive.zipped.name:
            archive.original_filename = os.path.basename(archive.zipped.name)

        if archive.thumbnail and (not archive.thumbnail_width or not archive.thumbnail_height):
            try:
                if os.path.isfile(archive.thumbnail.path):
                    archive.thumbnail = archive.thumbnail.name
            except Exception:
                pass

        if save:
            archive.save(force_update=True)
        return True
