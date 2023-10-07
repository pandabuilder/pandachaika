import logging
import os
from typing import Optional, Any

from core.base.types import DataDict
from core.base.utilities import available_filename, calc_crc32, get_zip_fileinfo_for_gallery, \
    construct_request_dict, request_with_retries, get_base_filename_string_from_gallery_data, replace_illegal_name

from . import constants
from .utilities import ChaikaGalleryData

from core.downloaders.handlers import BaseDownloader
from viewer.models import Archive

logger = logging.getLogger(__name__)


class PandaBackupHttpFileDownloader(BaseDownloader):

    type = 'archive'
    archive_only = True
    provider = constants.provider_name

    def start_download(self) -> None:

        if not self.gallery or not self.gallery.temp_archive:
            return

        logger.info("Downloading an archive: {} from a Panda Backup-like source: {}".format(
            self.gallery.title,
            self.gallery.temp_archive['link']
        ))

        to_use_filename = get_base_filename_string_from_gallery_data(self.gallery)

        to_use_filename = replace_illegal_name(to_use_filename)

        self.gallery.filename = available_filename(
            self.settings.MEDIA_ROOT,
            os.path.join(
                self.own_settings.archive_dl_folder,
                to_use_filename + '.zip'))  # TODO: File could be cbz.

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict['stream'] = True
        request_file = request_with_retries(
            self.gallery.temp_archive['link'],
            request_dict,
        )
        if not request_file:
            logger.error("Could not download archive")
            self.return_code = 0
            return
        filepath = os.path.join(self.settings.MEDIA_ROOT, self.gallery.filename)

        with open(filepath, 'wb') as fo:
            for chunk in request_file.iter_content(4096):
                fo.write(chunk)

        self.gallery.filesize, self.gallery.filecount = get_zip_fileinfo_for_gallery(filepath)
        if self.gallery.filesize > 0:
            self.crc32 = calc_crc32(filepath)

            self.fileDownloaded = 1
            self.return_code = 1

        else:
            logger.error("Could not download archive")
            self.return_code = 0

    def update_archive_db(self, default_values: DataDict) -> Optional['Archive']:

        if not self.gallery or not self.gallery.temp_archive:
            return None

        values = {
            'title': self.gallery.title,
            'title_jpn': self.gallery.title_jpn,
            'zipped': self.gallery.filename,
            'crc32': self.crc32,
            'filesize': self.gallery.filesize,
            'filecount': self.gallery.filecount,
            'source_type': self.gallery.temp_archive['source'],
            'reason': self.gallery.temp_archive['reason']
        }
        default_values.update(values)
        return Archive.objects.update_or_create_by_values_and_gid(
            default_values,
            (self.gallery.gid, self.gallery.provider),
            zipped=self.gallery.filename
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.gallery: Optional[ChaikaGalleryData] = None


API = (
    PandaBackupHttpFileDownloader,
)
