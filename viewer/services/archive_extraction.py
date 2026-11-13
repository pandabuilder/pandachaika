import logging
import mimetypes
import os
import shutil
import typing
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
        extracted_images = list(archive.image_set.filter(extracted=True))

        if extracted_images:
            for img in extracted_images:
                img.image.delete(save=False)
                img.image = None
                img.thumbnail.delete(save=False)
                img.thumbnail = None
                img.extracted = False

            from viewer.models import Image

            Image.objects.bulk_update(extracted_images, ["image", "thumbnail", "extracted"])

        archive.extracted = False
        archive.simple_save()

    @classmethod
    def reduce(cls, archive: "Archive") -> bool:
        """Idempotently reduce extracted archive images back to compressed state."""
        if not archive.extracted:
            return False

        extracted_images = list(archive.image_set.filter(extracted=True))

        for img in extracted_images:
            img.image.delete(save=False)
            img.image = None
            img.thumbnail.delete(save=False)
            img.thumbnail = None
            img.extracted = False

        if extracted_images:
            from viewer.models import Image

            Image.objects.bulk_update(extracted_images, ["image", "thumbnail", "extracted"])

        archive.extracted = False
        archive.simple_save()
        return True

    @classmethod
    def extract(cls, archive: "Archive", resized: bool = False, skip_zip_check: bool = False) -> bool:
        """Extract images from the archive's zip file to the media storage."""
        if archive.extracted:
            return False
        if not archive.zipped.name or not os.path.isfile(archive.zipped.path):
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

        os.makedirs(pjoin(settings.MEDIA_ROOT, f"images/extracted/archive_{archive.pk}/full/"), exist_ok=True)
        os.makedirs(pjoin(settings.MEDIA_ROOT, f"images/extracted/archive_{archive.pk}/thumb/"), exist_ok=True)

        non_extracted_images = archive.image_set.filter(extracted=False)
        if not non_extracted_images.exists():
            archive.generate_image_set(skip_zip_check=True)
            non_extracted_images = archive.image_set.filter(extracted=False)
        images_by_position = {img.archive_position: img for img in non_extracted_images}

        filtered_files = get_images_from_zip(my_zip)

        from viewer.models import Image, upload_imgpath, upload_thumbpath_handler

        activity_cb = getattr(archive, "_activity_callback", None)
        images_to_update: list[Image] = []

        for count, filename_tuple in enumerate(filtered_files, start=1):
            if callable(activity_cb):
                activity_cb()
            if count not in images_by_position:
                archive.generate_image_set(skip_zip_check=True)
                images_by_position = {img.archive_position: img for img in archive.image_set.filter(extracted=False)}
                if count not in images_by_position:
                    continue
            image = images_by_position[count]
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
            if not image.image_height and raw_full_im.size:
                image.image_width, image.image_height = raw_full_im.size
            raw_full_im.close()
            thumb_im.close()
            image.thumbnail = thumb_img_name
            if im_size:
                image.thumbnail_width, image.thumbnail_height = im_size

            image.extracted = True
            images_to_update.append(image)

        if images_to_update:
            Image.objects.bulk_update(
                images_to_update,
                [
                    "image",
                    "thumbnail",
                    "thumbnail_width",
                    "thumbnail_height",
                    "image_width",
                    "image_height",
                    "extracted",
                ],
            )

        my_zip.close()
        archive.extracted = True
        archive.simple_save()
        return True

    @classmethod
    def is_image_extracted_and_not_resized(cls, image: "Image") -> bool:
        """Check if an image is extracted on the filesystem in its original, unresized resolution."""
        if not image.extracted or not image.image:
            return False

        try:
            file_path = image.image.path
            if not os.path.isfile(file_path):
                return False
        except (ValueError, AttributeError):
            return False

        # If original dimensions are recorded, compare against extracted dimensions
        if image.original_width and image.original_height:
            w = image.image_width
            h = image.image_height
            if not w or not h:
                try:
                    with PImage.open(file_path) as im:
                        w, h = im.size
                except Exception:
                    return False
            if w != image.original_width or h != image.original_height:
                return False

        # If original file size is recorded, compare against disk file size
        # (shutil.copyfileobj preserves exact original size, while resized re-encodes)
        if image.image_size:
            try:
                if os.path.getsize(file_path) != image.image_size:
                    return False
            except OSError:
                return False
        elif not (image.original_width and image.original_height):
            # Neither dimensions nor size were cached on the model. Check against original zip if available.
            try:
                archive = image.archive
                if archive and archive.zipped.name and os.path.isfile(archive.zipped.path):
                    with zipfile.ZipFile(archive.zipped.path, "r") as my_zip:
                        filtered_files = get_images_from_zip(my_zip)
                        target = None
                        if image.archive_position and 1 <= image.archive_position <= len(filtered_files):
                            target = filtered_files[image.archive_position - 1]
                        elif image.image_name:
                            for f in filtered_files:
                                if os.path.split(f[2].replace("\\", os.sep))[1] == image.image_name:
                                    target = f
                                    break
                        if target:
                            if target[1] is None:
                                expected_size = my_zip.getinfo(target[0]).file_size
                            else:
                                with my_zip.open(target[1]) as nested_f:
                                    with zipfile.ZipFile(nested_f) as nested_zip:
                                        expected_size = nested_zip.getinfo(target[0]).file_size
                            if os.path.getsize(file_path) != expected_size:
                                return False
            except Exception:
                return False

        return True

    @classmethod
    def get_image_zip_stream(
        cls, image: "Image", chunk_size: int = 64 * 1024
    ) -> tuple[typing.Callable[[], typing.Generator[bytes, None, None]], str, str | None, int | None] | None:
        """
        Locate image in archive zip and return (stream_generator_factory, filename, content_type, file_size).
        Returns None if zip or image cannot be found.
        """
        archive = image.archive
        if not archive.zipped.name or not os.path.isfile(archive.zipped.path):
            return None

        zip_path = archive.zipped.path
        try:
            with zipfile.ZipFile(zip_path, "r") as my_zip:
                if my_zip.testzip():
                    return None
                filtered_files = get_images_from_zip(my_zip)
                if not filtered_files:
                    return None

                target_tuple = None
                if image.archive_position and 1 <= image.archive_position <= len(filtered_files):
                    candidate = filtered_files[image.archive_position - 1]
                    cand_name = os.path.split(candidate[2].replace("\\", os.sep))[1]
                    if (
                        not image.image_name
                        or cand_name == image.image_name
                        or os.path.basename(candidate[0]) == image.image_name
                    ):
                        target_tuple = candidate

                if not target_tuple and image.image_name:
                    for cand in filtered_files:
                        cand_name = os.path.split(cand[2].replace("\\", os.sep))[1]
                        if cand_name == image.image_name or os.path.basename(cand[0]) == image.image_name:
                            target_tuple = cand
                            break

                if not target_tuple and 1 <= image.position <= len(filtered_files):
                    target_tuple = filtered_files[image.position - 1]

                if not target_tuple:
                    return None

                file_size = None
                try:
                    if target_tuple[1] is None:
                        file_size = my_zip.getinfo(target_tuple[0]).file_size
                    else:
                        with my_zip.open(target_tuple[1]) as nested_f:
                            with zipfile.ZipFile(nested_f) as nested_zip:
                                file_size = nested_zip.getinfo(target_tuple[0]).file_size
                except Exception:
                    file_size = image.image_size

                filename = image.image_name or os.path.basename(target_tuple[0])
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        except Exception as e:
            logger.error("Error reading zip file for archive %s: %s", archive.pk, e)
            return None

        def stream_generator() -> typing.Generator[bytes, None, None]:
            with zipfile.ZipFile(zip_path, "r") as zf:
                if target_tuple[1] is None:
                    with zf.open(target_tuple[0]) as current_img:
                        while chunk := current_img.read(chunk_size):
                            yield chunk
                else:
                    with zf.open(target_tuple[1]) as current_zip:
                        with zipfile.ZipFile(current_zip) as my_nested_zip:
                            with my_nested_zip.open(target_tuple[0]) as current_img:
                                while chunk := current_img.read(chunk_size):
                                    yield chunk

        return stream_generator, filename, content_type, file_size

