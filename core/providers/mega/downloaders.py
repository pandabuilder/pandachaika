import logging
import os
import shutil
import subprocess
from tempfile import mkdtemp
from typing import Any, Optional

from core.base.types import DataDict
from core.base.utilities import replace_illegal_name, available_filename, calc_crc32, get_zip_fileinfo

from core.downloaders.handlers import BaseDownloader
from viewer.models import Archive

logger = logging.getLogger(__name__)


class MegaArchiveDownloader(BaseDownloader):

    type = 'archive'
    provider = 'mega'
    archive_only = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    def start_download(self) -> None:

        if not self.gallery or not self.gallery.link:
            return

        if self.own_settings.megadl_executable_path:
            exe_path_to_use = shutil.which(self.own_settings.megadl_executable_path)
        else:
            exe_path_to_use = shutil.which(self.own_settings.megadl_executable_name)

        if not exe_path_to_use:
            self.return_code = 0
            logger.error("The megadl tools was not found")
            return

        directory_path = mkdtemp()

        arguments = ["dl", "--no-progress", "--print-names", "--path", "{}".format(
            directory_path
        )]

        if self.own_settings.proxy:
            arguments.append("--proxy")
            arguments.append("{}".format(self.own_settings.proxy))

        if self.own_settings.extra_megadl_arguments:
            arguments.append("{}".format(self.own_settings.extra_megadl_arguments))

        arguments.append("{}".format(self.gallery.link))

        logger.info("Calling megatools: {}.".format(" ".join([exe_path_to_use, *arguments])))

        process_result = subprocess.run(
            [exe_path_to_use, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        message_text = process_result.stdout

        if not message_text:
            self.return_code = 0
            logger.error("The link could not be downloaded, no output was generated after running megadl")
            return

        if process_result.stderr:
            self.return_code = 0
            logger.error("An error was captured when running megadl: {}".format(process_result.stderr))
            return

        if "WARNING: Skipping invalid" in message_text:
            self.return_code = 0
            logger.error("The link could not be downloaded: {}".format(message_text))
            return

        # If we downloaded a folder, just take the first result. Folder returns full path, tested with megatools 1.11.0
        file_names = message_text.splitlines()
        file_name = os.path.basename(file_names[0])

        output_path = file_names[0]

        if not os.path.isfile(output_path):
            self.return_code = 0
            logger.error("The resulting download file was not found: {}".format(file_name))
            return

        self.gallery.filename = available_filename(
            self.settings.MEDIA_ROOT,
            os.path.join(
                self.own_settings.archive_dl_folder,
                replace_illegal_name(file_name)
            )
        )

        self.gallery.title = os.path.splitext(file_name)[0]

        filepath = os.path.join(self.settings.MEDIA_ROOT,
                                self.gallery.filename)

        shutil.move(output_path, filepath)
        shutil.rmtree(directory_path, ignore_errors=True)

        self.gallery.filesize, self.gallery.filecount = get_zip_fileinfo(filepath)
        if self.gallery.filesize > 0:
            self.crc32 = calc_crc32(filepath)

            self.fileDownloaded = 1
            self.return_code = 1

        else:
            logger.error("Could not download archive")
            self.return_code = 0

    def update_archive_db(self, default_values: DataDict) -> Optional['Archive']:

        if not self.gallery:
            return None

        values = {
            'title': self.gallery.title,
            'title_jpn': '',
            'zipped': self.gallery.filename,
            'crc32': self.crc32,
            'filesize': self.gallery.filesize,
            'filecount': self.gallery.filecount,
        }
        default_values.update(values)
        return Archive.objects.update_or_create_by_values_and_gid(
            default_values,
            None,
            zipped=self.gallery.filename
        )


API = (
    MegaArchiveDownloader,
)
