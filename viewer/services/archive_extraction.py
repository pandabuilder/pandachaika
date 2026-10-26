import logging
import os
import shutil
import typing
import uuid
import zipfile
import zlib
from os.path import join as pjoin

from PIL import Image as PImage, ImageOps
from django.conf import settings

from core.base.image_ops import img_to_thumbnail
from core.base.utilities import get_images_from_zip

if typing.TYPE_CHECKING:
    from viewer.models import Archive, Image

logger = logging.getLogger(__name__)


class ArchiveExtractionService:
    """Service handling archive extraction, reduction, and zip safety checks."""

    @staticmethod
    def validate_zip_archive(zip_path: str) -> tuple[bool, str]:
        """Validate zip integrity and ensure no path traversal / Zip Slip entries exist."""
        if not os.path.isfile(zip_path):
            return False, f"Zip file does not exist: {zip_path}"

        try:
            with zipfile.ZipFile(zip_path, "r") as my_zip:
                bad_file = my_zip.testzip()
                if bad_file:
                    return False, f"Zip CRC check failed on member: {bad_file}"

                for member in my_zip.namelist():
                    # Reject absolute paths or paths containing traversal components
                    normalized = os.path.normpath(member)
                    if (
                        member.startswith("/")
                        or member.startswith("\\")
                        or normalized.startswith("..")
                        or "/../" in member
                        or "\\..\\" in member
                    ):
                        return False, f"Malicious path traversal detected in zip member: {member}"

        except (zipfile.BadZipFile, NotImplementedError, zlib.error) as e:
            return False, f"Invalid or corrupted zip archive: {e}"

        return True, ""

    @classmethod
    def extract_delete(cls, archive: "Archive") -> None:
        """Delete all extracted image files for the given archive."""
        extracted_images = archive.image_set.filter(extracted=True)

        if extracted_images:
            for img in extracted_images:
                img.image.delete(save=False)
                img.image = None
                img.thumbnail.delete(save=False)
                img.thumbnail = None
                img.extracted = False
                img.save()

        archive.extracted = False
        archive.simple_save()

    @classmethod
    def reduce(cls, archive: "Archive") -> bool:
        """Idempotently reduce extracted archive images back to compressed state."""
        if not archive.extracted:
            return False

        extracted_images = archive.image_set.filter(extracted=True)

        for img in extracted_images:
            img.image.delete(save=False)
            img.image = None
            img.thumbnail.delete(save=False)
            img.thumbnail = None
            img.extracted = False
            img.save()

        archive.extracted = False
        archive.simple_save()
        return True

    @classmethod
    def extract(cls, archive: "Archive", resized: bool = False) -> bool:
        """Extract images from the archive's zip file to the media storage."""
        if archive.extracted:
            return False
        if not archive.zipped.name or not os.path.isfile(archive.zipped.path):
            return False

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

        os.makedirs(pjoin(settings.MEDIA_ROOT, f"images/extracted/archive_{archive.pk}/full/"), exist_ok=True)
        os.makedirs(pjoin(settings.MEDIA_ROOT, f"images/extracted/archive_{archive.pk}/thumb/"), exist_ok=True)

        non_extracted_images = archive.image_set.filter(extracted=False)
        if not non_extracted_images:
            archive.generate_image_set()
            non_extracted_images = archive.image_set.filter(extracted=False)
        non_extracted_positions = non_extracted_images.values_list("archive_position", flat=True)

        filtered_files = get_images_from_zip(my_zip)

        from viewer.models import Image, upload_imgpath, upload_thumbpath_handler

        for count, filename_tuple in enumerate(filtered_files, start=1):
            if count not in non_extracted_positions:
                continue
            try:
                image: "Image" = non_extracted_images.get(archive_position=count)
            except Image.DoesNotExist:
                archive.generate_image_set()
                image = non_extracted_images.get(archive_position=count)
            image_name = os.path.split(filename_tuple[2].replace("\\", os.sep))[1]

            # Image
            img_path = upload_imgpath(archive, image_name)
            full_img_name = pjoin(settings.MEDIA_ROOT, img_path)
            thumb_img_name = upload_thumbpath_handler(image, image_name)

            with (
                open(full_img_name, "wb") as current_new_img,
                my_zip.open(filename_tuple[1] or filename_tuple[0]) as current_file,
            ):
                if filename_tuple[1]:
                    with zipfile.ZipFile(current_file) as my_nested_zip:
                        with my_nested_zip.open(filename_tuple[0]) as current_img:
                            if resized:
                                raw_im = PImage.open(current_img)
                                transposed_im = ImageOps.exif_transpose(raw_im)
                                im_resized = (
                                    transposed_im.convert("RGB")
                                    if transposed_im.mode != "RGB"
                                    else transposed_im
                                )
                                im_w, im_h = im_resized.size
                                if im_w > im_h:
                                    im_resized.thumbnail(
                                        (settings.CRAWLER_SETTINGS.horizontal_image_max_width, 9999),
                                        PImage.Resampling.LANCZOS,
                                    )
                                else:
                                    im_resized.thumbnail(
                                        (settings.CRAWLER_SETTINGS.vertical_image_max_width, 9999),
                                        PImage.Resampling.LANCZOS,
                                    )
                                im_resized.save(current_new_img, "JPEG")
                            else:
                                shutil.copyfileobj(current_img, current_new_img)
                else:
                    if resized:
                        raw_im = PImage.open(current_file)
                        transposed_im = ImageOps.exif_transpose(raw_im)
                        im_resized = (
                            transposed_im.convert("RGB")
                            if transposed_im.mode != "RGB"
                            else transposed_im
                        )
                        im_w, im_h = im_resized.size
                        if im_w > im_h:
                            im_resized.thumbnail(
                                (settings.CRAWLER_SETTINGS.horizontal_image_max_width, 9999),
                                PImage.Resampling.LANCZOS,
                            )
                        else:
                            im_resized.thumbnail(
                                (settings.CRAWLER_SETTINGS.vertical_image_max_width, 9999),
                                PImage.Resampling.LANCZOS,
                            )
                        im_resized.save(current_new_img, "JPEG")
                    else:
                        shutil.copyfileobj(current_file, current_new_img)
            image.image.name = img_path

            # Thumbnail
            raw_full_im = PImage.open(full_img_name)
            thumb_im = img_to_thumbnail(raw_full_im)
            thumb_im.save(pjoin(settings.MEDIA_ROOT, thumb_img_name), "JPEG")
            im_size = thumb_im.size
            raw_full_im.close()
            thumb_im.close()
            image.thumbnail = thumb_img_name
            if im_size:
                image.thumbnail_width, image.thumbnail_height = im_size

            image.extracted = True
            image.save()

        my_zip.close()
        archive.extracted = True
        archive.simple_save()
        return True
