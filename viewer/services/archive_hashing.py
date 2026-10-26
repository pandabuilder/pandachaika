import io
import logging
import os
import typing
import zipfile
import zlib

from django.conf import settings
from django.contrib.contenttypes.models import ContentType

from core.base.types import ArchiveStatisticsCalculator
from core.base.utilities import calc_crc32, get_images_from_zip, sha1_from_file_object

if typing.TYPE_CHECKING:
    from viewer.models import Archive

logger = logging.getLogger(__name__)


class ArchiveHashingService:
    """Service handling SHA1, pHash, and CRC32 hash calculations for archives and images."""

    @classmethod
    def calculate_archive_crc32(cls, archive: "Archive", use_pool: bool = True) -> str:
        """Compute the CRC32 checksum of the archive's zip file."""
        if not archive.zipped.name or not os.path.isfile(archive.zipped.path):
            return ""
        return calc_crc32(archive.zipped.path, use_pool=use_pool)

    @classmethod
    def calculate_sha1_and_data_for_images(
        cls,
        archive: "Archive",
        process_image_data: bool = True,
        process_other_data: bool = True,
        process_archive_statistics: bool = True,
    ) -> bool:
        """Compute SHA1, dimensions, pHash, and metadata for images within the archive."""
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

        from viewer.models import ArchiveStatistics, Image, ItemProperties

        image_set = archive.image_set.all().order_by("archive_position")
        filtered_files = get_images_from_zip(my_zip)
        image_type = ContentType.objects.get_for_model(Image)

        archive_statistics = None
        archive_stats_calc = None
        if process_archive_statistics:
            archive_statistics, _ = ArchiveStatistics.objects.get_or_create(archive=archive)
            archive_stats_calc = ArchiveStatisticsCalculator()

        images_to_update = []
        phash_tasks = []

        for filename_tuple, image in zip(filtered_files, image_set):
            image_filename, nested_zip_filename, _ = filename_tuple

            if not image:
                continue

            if process_image_data and settings.CRAWLER_SETTINGS.auto_phash_images:
                phash_tasks.append((archive.zipped.path, image_filename, nested_zip_filename))

            if nested_zip_filename is None:
                with my_zip.open(image_filename) as current_zip_img:
                    if process_image_data:
                        image.sha1 = sha1_from_file_object(current_zip_img)
                        image.set_attributes_from_image(
                            current_zip_img,
                            my_zip.getinfo(image_filename).file_size,
                            os.path.basename(image_filename),
                        )
            else:
                with my_zip.open(nested_zip_filename) as current_zip:
                    nested_zip_data = io.BytesIO(current_zip.read())
                    with zipfile.ZipFile(nested_zip_data) as my_nested_zip:
                        with my_nested_zip.open(image_filename) as current_zip_img:
                            if process_image_data:
                                image.sha1 = sha1_from_file_object(current_zip_img)
                                image.set_attributes_from_image(
                                    current_zip_img,
                                    my_nested_zip.getinfo(image_filename).file_size,
                                    os.path.basename(image_filename),
                                )

            if process_image_data:
                images_to_update.append(image)

            if process_archive_statistics and archive_stats_calc is not None:
                archive_stats_calc.set_values(
                    filesize=image.image_size,
                    height=image.original_height,
                    width=image.original_width,
                    image_mode=image.image_mode,
                    is_horizontal=image.image_width / image.image_height > 1
                    if image.image_width and image.image_height
                    else False,
                    file_type=os.path.splitext(image_filename)[1],
                )

        phash_results = {}
        if phash_tasks:
            from core.base.process_pool import calculate_phash_for_zip_members_in_pool

            phash_results = calculate_phash_for_zip_members_in_pool(phash_tasks)

        if images_to_update:
            Image.objects.bulk_update(
                images_to_update,
                ["sha1", "image_size", "original_height", "original_width", "image_mode", "image_name"],
            )

        if phash_results:
            filename_to_image_map = {image.image_name: image for image in image_set}

            props_to_create = []
            props_to_update = []
            existing_props_qs = ItemProperties.objects.filter(
                content_type=image_type,
                object_id__in=[img.pk for img in filename_to_image_map.values()],
                tag="hash-compare",
                name="phash",
            )
            existing_props_map = {prop.object_id: prop for prop in existing_props_qs}

            for filename, hash_val in phash_results.items():
                image_from_name = filename_to_image_map.get(filename)
                if not image_from_name:
                    continue

                if image_from_name.pk in existing_props_map:
                    prop = existing_props_map[image_from_name.pk]
                    prop.value = hash_val
                    props_to_update.append(prop)
                else:
                    props_to_create.append(
                        ItemProperties(
                            content_type=image_type,
                            object_id=image_from_name.pk,
                            tag="hash-compare",
                            name="phash",
                            value=hash_val,
                        )
                    )

            if props_to_create:
                ItemProperties.objects.bulk_create(props_to_create)
            if props_to_update:
                ItemProperties.objects.bulk_update(props_to_update, ["value"])

        if process_archive_statistics and archive_stats_calc is not None and archive_statistics is not None:
            archive_statistics.filesize_average = archive_stats_calc.mean("filesize")
            archive_statistics.height_average = archive_stats_calc.mean("height")
            archive_statistics.width_average = archive_stats_calc.mean("width")
            archive_statistics.height_mode = archive_stats_calc.mode("height")
            archive_statistics.width_mode = archive_stats_calc.mode("width")
            archive_statistics.height_stddev = archive_stats_calc.stddev("height")
            archive_statistics.width_stddev = archive_stats_calc.stddev("width")
            archive_statistics.is_horizontal_mode = archive_stats_calc.mode("is_horizontal") or False
            archive_statistics.image_mode_mode = archive_stats_calc.mode("image_mode")
            archive_statistics.file_type_mode = archive_stats_calc.mode("file_type")
            archive_statistics.file_type_match = archive_stats_calc.eq_to_value(
                "file_type", archive_statistics.file_type_mode
            )
            archive_statistics.save()

        if process_other_data and archive.archivefileentry_set.exists():
            for file_entry in archive.archivefileentry_set.all():
                with my_zip.open(file_entry.file_name) as current_other_file:
                    file_entry.sha1 = sha1_from_file_object(current_other_file)
                    file_entry.save()

        my_zip.close()
        return True

    @classmethod
    def hash_images_with_function(
        cls, archive: "Archive", hashing_function: typing.Callable[[typing.IO], str]
    ) -> dict[int, str]:
        """Hash all images in the archive with an arbitrary hashing function."""
        image_set = archive.image_set.all()
        image_result: dict[int, str] = {}

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
                return image_result
        except (zipfile.BadZipFile, NotImplementedError, zlib.error) as e:
            logger.error(
                "Error opening or testing zipfile for archive: %s on file: %s: %s",
                archive,
                archive.zipped.path,
                e,
            )
            if my_zip:
                my_zip.close()
            return image_result

        filtered_files = get_images_from_zip(my_zip)

        for count, filename_tuple in enumerate(filtered_files, start=1):
            image = image_set.get(archive_position=count)
            if image.extracted:
                with open(image.image.path, "rb") as current_img:
                    image_result[count] = hashing_function(current_img)
            else:
                if filename_tuple[1] is None:
                    with my_zip.open(filename_tuple[0]) as current_zip_img:
                        image_result[count] = hashing_function(current_zip_img)
                else:
                    with my_zip.open(filename_tuple[1]) as current_zip:
                        with zipfile.ZipFile(current_zip) as my_nested_zip:
                            with my_nested_zip.open(filename_tuple[0]) as current_zip_img:
                                image_result[count] = hashing_function(current_zip_img)

        my_zip.close()
        return image_result
