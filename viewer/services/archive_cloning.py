import itertools
import logging
import os
import shlex
import shutil
import subprocess
import typing
import zipfile
import zlib
from tempfile import mkdtemp

from django.conf import settings

from core.base.utilities import available_filename, file_matches_any_filter, get_images_from_zip

if typing.TYPE_CHECKING:
    from core.base.setup import CloningImageToolSettings
    from viewer.models import Archive, Image

logger = logging.getLogger(__name__)


class ArchiveCloningService:
    """Service handling archive cloning, reordering, and splitting operations."""

    @classmethod
    def create_new_archive_ordered_by_sha1(
        cls, archive: "Archive", sha1s: list[str]
    ) -> "tuple[Archive | None, str]":
        """Create a new archive reordered according to the provided list of image SHA1 hashes."""
        images = archive.image_set.filter(sha1__isnull=False)
        if images.count() != archive.filecount:
            return None, "Image count different from filecount"

        if images.count() != len(sha1s):
            return None, "Image count different from SHA1 values"

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
                return None, f"Bad original zip file CRC: {bad_file}"
        except (zipfile.BadZipFile, NotImplementedError, zlib.error) as e:
            logger.error(
                "Error opening or testing zipfile for archive: %s on file: %s: %s",
                archive,
                archive.zipped.path,
                e,
            )
            if my_zip:
                my_zip.close()
            return None, f"Bad original zip file: {e}"

        filtered_files = get_images_from_zip(my_zip)

        if len(sha1s) != len(filtered_files):
            my_zip.close()
            return None, "SHA1 values count different from filtered files from zip file"

        if not archive.zipped.name:
            my_zip.close()
            return None, "Original file is not set"

        new_file_name = available_filename(settings.MEDIA_ROOT, archive.zipped.name)
        new_file_path = os.path.join(settings.MEDIA_ROOT, new_file_name)
        new_zipfile = zipfile.ZipFile(new_file_path, "w")

        for count, sha1 in enumerate(sha1s, start=1):
            current_image = images.filter(sha1=sha1).first()
            if current_image is None:
                new_zipfile.close()
                my_zip.close()
                return None, f"Image from SHA1 value: {sha1} does not exist"

            archive_position = current_image.archive_position
            current_file_tuple = filtered_files[archive_position - 1]
            if current_file_tuple[1] is None:
                with my_zip.open(current_file_tuple[0]) as current_zip_img:
                    current_basename = os.path.basename(current_file_tuple[0])
                    new_zipfile.writestr(f"{str(count).zfill(4)}_{current_basename}", current_zip_img.read())
            else:
                with my_zip.open(current_file_tuple[1]) as current_zip:
                    with zipfile.ZipFile(current_zip) as my_nested_zip:
                        with my_nested_zip.open(current_file_tuple[0]) as current_zip_img:
                            current_basename = os.path.basename(current_file_tuple[0])
                            new_zipfile.writestr(
                                f"{str(count).zfill(4)}_{current_basename}", current_zip_img.read()
                            )

        new_zipfile.close()
        my_zip.close()

        from viewer.models import Archive

        new_archive = archive
        possible_matches = archive.possible_matches.all()
        tags = archive.tags.all()
        alternative_sources = archive.alternative_sources.all()

        new_archive.pk = None
        new_archive._state.adding = True
        new_archive.extracted = False
        new_archive.public = False
        new_archive.public_date = None
        new_archive.crc32 = ""
        new_archive.thumbnail = ""
        new_archive.zipped = new_file_name
        new_archive.ingest()
        new_archive.possible_matches.set(possible_matches)
        new_archive.tags.set(tags)
        new_archive.alternative_sources.set(alternative_sources)

        return new_archive, ""

    @staticmethod
    def build_image_tool_command(command_template: str, input_path: str, output_path: str) -> list[str]:
        """Build an argv list for an external image tool without invoking a shell.

        The template is tokenized with shlex *before* substituting the placeholders, so
        file paths (which may originate from untrusted archive member names) always end up
        as single, literal arguments and can never inject shell syntax or extra arguments.
        """
        tokens = shlex.split(command_template)
        if not tokens:
            raise ValueError("Image tool command is empty")
        return [token.replace("{input}", input_path).replace("{output}", output_path) for token in tokens]

    @classmethod
    def run_image_tool(cls, image_tool: "CloningImageToolSettings", input_path: str, output_path: str) -> str | None:
        """Run the configured image tool on a single file. Returns an error message or None on success."""
        try:
            command = cls.build_image_tool_command(image_tool.executable_path, input_path, output_path)
        except ValueError as e:
            return f"Invalid command configured for {image_tool.name}: {e}"

        try:
            process_result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                shell=False,
            )
        except (FileNotFoundError, PermissionError):
            return f"The following command could not run: {image_tool.name}"

        if process_result.returncode != 0:
            return f"An error was captured when running {image_tool.name}: {process_result.stderr}"
        return None

    @classmethod
    def clone_archive_plus(
        cls,
        archive: "Archive",
        sha1s: list[str] | None = None,
        image_tool: "CloningImageToolSettings | None" = None,
    ) -> "tuple[Archive | None, str]":
        """Clone an archive optionally applying image transformation tools or SHA1 ordering."""
        images = archive.image_set.filter(sha1__isnull=False)
        if images.count() != archive.filecount:
            return None, "Image count different from filecount"

        if sha1s:
            local_sha1s = sha1s
        else:
            local_sha1s = [x for x in images.values_list("sha1", flat=True) if x is not None]

        if images.count() != len(local_sha1s):
            return None, "Image count different from SHA1 values"

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
                return None, f"Bad original zip file CRC: {bad_file}"
        except (zipfile.BadZipFile, NotImplementedError, zlib.error) as e:
            logger.error(
                "Error opening or testing zipfile for archive: %s on file: %s: %s",
                archive,
                archive.zipped.path,
                e,
            )
            if my_zip:
                my_zip.close()
            return None, f"Bad original zip file: {e}"

        filtered_files = get_images_from_zip(my_zip)
        if len(local_sha1s) != len(filtered_files):
            my_zip.close()
            return None, "SHA1 values count different from filtered files from zip file"

        if not archive.zipped.name:
            my_zip.close()
            return None, "Original file is not set"

        new_file_name = available_filename(settings.MEDIA_ROOT, archive.zipped.name)
        new_file_path = os.path.join(settings.MEDIA_ROOT, new_file_name)
        new_zipfile = zipfile.ZipFile(new_file_path, "w")
        dir_path = mkdtemp(dir=settings.CRAWLER_SETTINGS.temp_directory_path)

        def abort(message: str) -> "tuple[Archive | None, str]":
            new_zipfile.close()
            my_zip.close()
            shutil.rmtree(dir_path, ignore_errors=True)
            if os.path.isfile(new_file_path):
                os.remove(new_file_path)
            return None, message

        for count, sha1 in enumerate(local_sha1s, start=1):
            try:
                current_image = images.get(sha1=sha1)
            except Exception:
                return abort(f"Image from SHA1 value: {sha1} does not exist")

            archive_position = current_image.archive_position
            current_file_tuple = filtered_files[archive_position - 1]

            if current_file_tuple[1] is None:
                # ZipFile.extract() sanitizes member names (strips '..' and absolute paths),
                # so always use the returned path instead of re-joining the raw member name.
                extracted_file = my_zip.extract(current_file_tuple[0], path=dir_path)
            else:
                with my_zip.open(current_file_tuple[1]) as current_zip:
                    with zipfile.ZipFile(current_zip) as my_nested_zip:
                        extracted_file = my_nested_zip.extract(current_file_tuple[0], path=dir_path)

            if image_tool and file_matches_any_filter(current_file_tuple[0], image_tool.file_filters):
                mod_file = os.path.join(dir_path, f"mod_file_{count}.tmp")
                error_message = cls.run_image_tool(image_tool, extracted_file, mod_file)
                if error_message:
                    return abort(error_message)
                out_file = mod_file
            else:
                out_file = extracted_file

            current_basename = os.path.basename(current_file_tuple[0])
            out_name = f"{str(count).zfill(4)}_{current_basename}" if sha1s else current_basename
            new_zipfile.write(out_file, arcname=out_name)

        new_zipfile.close()
        my_zip.close()
        shutil.rmtree(dir_path, ignore_errors=True)

        new_archive = archive
        possible_matches = archive.possible_matches.all()
        tags = archive.tags.all()
        alternative_sources = archive.alternative_sources.all()

        new_archive.pk = None
        new_archive._state.adding = True
        new_archive.public = False
        new_archive.public_date = None
        new_archive.crc32 = ""
        new_archive.thumbnail = ""
        if image_tool:
            new_archive.filesize = None
        new_archive.extracted = False
        new_archive.zipped = new_file_name
        new_archive.ingest()
        new_archive.possible_matches.set(possible_matches)
        new_archive.tags.set(tags)
        new_archive.alternative_sources.set(alternative_sources)

        return new_archive, ""

    @classmethod
    def split_archive(
        cls,
        archive: "Archive",
        split_data: list[tuple[int, int, str | None, bool]],
        split_from_nested: bool = False,
    ) -> "tuple[list[Archive] | None, str]":
        """Split an archive into several sub-archives based on position ranges or nested zips."""
        images = archive.image_set.all()
        if images.count() != archive.filecount:
            return None, "Image count different from filecount"

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
                return None, f"Bad original zip file CRC: {bad_file}"
        except (zipfile.BadZipFile, NotImplementedError, zlib.error) as e:
            logger.error(
                "Error opening or testing zipfile for archive: %s on file: %s: %s",
                archive,
                archive.zipped.path,
                e,
            )
            if my_zip:
                my_zip.close()
            return None, f"Bad original zip file: {e}"

        if not archive.zipped.name:
            my_zip.close()
            return None, "Original file is not set"

        filtered_files = get_images_from_zip(my_zip)

        if split_from_nested:
            nested_images = [x for x in filtered_files if x[1] is not None]
            if len(filtered_files) == len(nested_images):
                split_data_to_use = []
                nested_with_index_images = [
                    (x[1][0], x[1][1], x[1][2], x[0] + 1) for x in enumerate(nested_images)
                ]
                for nested_file, image_group in itertools.groupby(nested_with_index_images, lambda x: x[1]):
                    image_list = list(image_group)
                    split_data_to_use.append((image_list[0][3], image_list[-1][3], nested_file, True))
            else:
                my_zip.close()
                return None, "Not all images are nested"
        else:
            split_data_to_use = split_data

        new_archives = []

        for split_entry in split_data_to_use:
            starting_position, ending_position, file_name, force_filename = split_entry
            current_dir, current_file = os.path.split(archive.zipped.name)
            if file_name and not force_filename and not file_name.endswith(".zip"):
                file_name += ".zip"
            new_name = file_name or current_file
            new_path = os.path.join(current_dir, new_name)
            new_file_name = available_filename(settings.MEDIA_ROOT, new_path)
            new_file_path = os.path.join(settings.MEDIA_ROOT, new_file_name)

            new_zipfile = zipfile.ZipFile(new_file_path, "w")
            dir_path = mkdtemp(dir=settings.CRAWLER_SETTINGS.temp_directory_path)

            for position in range(starting_position, ending_position + 1):
                try:
                    if split_from_nested:
                        current_image = images.get(archive_position=position)
                    else:
                        current_image = images.get(position=position)
                except Exception:
                    new_zipfile.close()
                    my_zip.close()
                    shutil.rmtree(dir_path, ignore_errors=True)
                    return None, f"Image from position: {position} does not exist"

                archive_position = current_image.archive_position
                current_file_tuple = filtered_files[archive_position - 1]
                if current_file_tuple[1] is None:
                    extracted_file = my_zip.extract(current_file_tuple[0], path=dir_path)
                    current_basename = os.path.basename(current_file_tuple[0])
                    new_zipfile.write(extracted_file, arcname=current_basename)
                else:
                    with my_zip.open(current_file_tuple[1]) as current_zip:
                        with zipfile.ZipFile(current_zip) as my_nested_zip:
                            extracted_file = my_nested_zip.extract(current_file_tuple[0], path=dir_path)
                            current_basename = os.path.basename(current_file_tuple[0])
                            new_zipfile.write(extracted_file, arcname=current_basename)

            new_zipfile.close()
            shutil.rmtree(dir_path, ignore_errors=True)

            new_archive = archive
            tags = archive.tags.all()
            alternative_sources = archive.alternative_sources.all()

            new_archive.pk = None
            new_archive._state.adding = True
            new_archive.public = False
            new_archive.public_date = None
            new_archive.title = new_name.replace(".zip", "")
            new_archive.crc32 = ""
            new_archive.thumbnail = ""
            new_archive.filesize = None
            new_archive.extracted = False
            new_archive.zipped = new_file_name
            new_archive.ingest()
            new_archive.tags.set(tags)
            new_archive.alternative_sources.set(alternative_sources)

            new_archive.refresh_from_db()
            new_archive.create_marks_for_similar_archives([archive.pk])
            new_archives.append(new_archive)

        my_zip.close()
        return new_archives, ""
