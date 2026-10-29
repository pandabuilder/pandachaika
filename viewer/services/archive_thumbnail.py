import logging
import os
import typing
import zipfile
import zlib
from collections import Counter
from os.path import join as pjoin

from PIL import Image as PImage
from django.conf import settings
from django.templatetags.static import static

from core.base.image_ops import img_to_thumbnail
from core.base.utilities import get_images_from_zip

if typing.TYPE_CHECKING:
    from viewer.models import Archive

logger = logging.getLogger(__name__)


class ArchiveThumbnailService:
    """Service handling archive thumbnail generation, cover extraction, and image preview data."""

    @classmethod
    def get_available_thumbnail_plus_size(cls, archive: "Archive") -> tuple[str, int | None, int | None]:
        """Return the best available thumbnail URL and dimensions for an archive."""
        if archive.thumbnail.name and archive.thumbnail.url:
            return archive.thumbnail.url, archive.thumbnail_height, archive.thumbnail_width
        elif archive.gallery and archive.gallery.thumbnail.name and archive.gallery.thumbnail.url:
            return (
                archive.gallery.thumbnail.url,
                archive.gallery.thumbnail_height,
                archive.gallery.thumbnail_width,
            )
        else:
            return static("imgs/no_cover.png"), 290, 196

    @classmethod
    def create_thumbnail_from_io_image(
        cls, archive: "Archive", current_img: typing.IO | typing.BinaryIO
    ) -> None:
        """Create and save a thumbnail from an in-memory image stream."""
        from viewer.models import thumb_path_handler

        raw_im = PImage.open(current_img)
        thumb_im = img_to_thumbnail(raw_im)
        thumb_name = thumb_path_handler(archive, "thumb2.jpg")
        os.makedirs(os.path.dirname(pjoin(settings.MEDIA_ROOT, thumb_name)), exist_ok=True)
        thumb_im.save(pjoin(settings.MEDIA_ROOT, thumb_name), "JPEG")
        im_size = thumb_im.size
        raw_im.close()
        thumb_im.close()
        archive.thumbnail = thumb_name
        if im_size:
            archive.thumbnail_width, archive.thumbnail_height = im_size

    @classmethod
    def generate_thumbnails(cls, archive: "Archive", use_pool: bool = True, skip_zip_check: bool = False) -> bool:
        """Generate cover thumbnails for an archive using a process pool or in-process fallback."""
        if not archive.zipped.name or not os.path.exists(archive.zipped.path):
            return False

        my_zip = None
        try:
            my_zip = zipfile.ZipFile(archive.zipped.path, "r")
            if not skip_zip_check:
                bad_file = my_zip.testzip()
                if bad_file:
                    logger.error(
                        "Zip file CRC check failed on file %s for archive: %s on file: %s",
                        bad_file,
                        archive,
                        archive.zipped.path,
                    )
                    my_zip.close()
                    return False
        except (zipfile.BadZipFile, NotImplementedError, zlib.error) as e:
            logger.error(
                "Error opening or testing zipfile for archive: %s on file: %s: %s",
                archive,
                archive.zipped.path,
                e,
            )
            if my_zip:
                my_zip.close()
            return False

        from viewer.models import ArchiveManageEntry, thumb_path_handler

        archive.thumbnail.delete(save=False)

        filtered_files = get_images_from_zip(my_zip)
        if not filtered_files:
            my_zip.close()
            return False

        nested_images = [x for x in filtered_files if x[1] is not None]
        if len(nested_images) > 0:
            c = Counter(nested_image[1] for nested_image in nested_images)
            mark_comment_list = [f"File: {x} has: {c[x]} images" for x in c.keys()]
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

        images_from_archive = archive.image_set.all()
        if images_from_archive:
            first_file = filtered_files[images_from_archive[0].archive_position - 1]
        else:
            first_file = filtered_files[0]

        thumb_name = thumb_path_handler(archive, "thumb2.jpg")
        output_path = pjoin(settings.MEDIA_ROOT, thumb_name)
        size = None

        if use_pool:
            try:
                from core.base.process_pool import generate_thumbnail_in_pool

                size = generate_thumbnail_in_pool(
                    archive.zipped.path, first_file[0], first_file[1], output_path
                )
            except Exception as e:
                logger.warning(
                    "generate_thumbnail_in_pool failed for archive %s, falling back to in-process: %s",
                    archive.pk,
                    e,
                )

        if size:
            archive.thumbnail = thumb_name
            archive.thumbnail_width, archive.thumbnail_height = size
        else:
            if first_file[1] is None:
                with my_zip.open(first_file[0]) as current_img:
                    cls.create_thumbnail_from_io_image(archive, current_img)
            else:
                with my_zip.open(first_file[1]) as current_zip:
                    with zipfile.ZipFile(current_zip) as my_nested_zip:
                        with my_nested_zip.open(first_file[0]) as current_img:
                            cls.create_thumbnail_from_io_image(archive, current_img)

        if settings.CRAWLER_SETTINGS.auto_phash_images:
            archive.create_or_update_thumbnail_hash("phash")

        my_zip.close()
        archive.simple_save()
        return True

    @classmethod
    def get_image_data_from_position(cls, archive: "Archive", position: int) -> bytes | None:
        """Return raw image bytes for an image at the given 1-based position."""
        real_position = position - 1
        if real_position < 0:
            return None
        if not archive.zipped.name or not os.path.exists(archive.zipped.path):
            return None

        try:
            with zipfile.ZipFile(archive.zipped.path, "r") as my_zip:
                if my_zip.testzip():
                    return None

                filtered_files = get_images_from_zip(my_zip)
                if not filtered_files:
                    return None

                images_from_archive = archive.image_set.all()
                if images_from_archive:
                    if real_position >= len(images_from_archive):
                        return None
                    first_file = filtered_files[images_from_archive[real_position].archive_position - 1]
                else:
                    if real_position >= len(filtered_files):
                        return None
                    first_file = filtered_files[real_position]

                if first_file[1] is None:
                    with my_zip.open(first_file[0]) as current_img:
                        return current_img.read()
                else:
                    with my_zip.open(first_file[1]) as current_zip:
                        with zipfile.ZipFile(current_zip) as my_nested_zip:
                            with my_nested_zip.open(first_file[0]) as current_img:
                                return current_img.read()
        except Exception as e:
            logger.error("Failed to read image at position %s for archive %s: %s", position, archive.pk, e)
            return None
