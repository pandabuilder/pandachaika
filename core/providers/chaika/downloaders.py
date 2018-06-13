import os
from typing import Optional

import requests

from core.base.types import DataDict
from core.base.utilities import replace_illegal_name, available_filename, calc_crc32, get_zip_filesize

from . import constants

from core.downloaders.handlers import BaseDownloader
from viewer.models import Archive


class PandaBackupHttpFileDownloader(BaseDownloader):

    type = 'archive'
    archive_only = True
    provider = constants.provider_name

    def start_download(self) -> None:

        if not self.gallery:
            return

        self.logger.info("Downloading an archive: {} from a Panda Backup-like source: {}".format(
            self.gallery.title,
            self.gallery.archiver_key
        ))

        self.gallery.title = replace_illegal_name(
            self.gallery.title)
        self.gallery.filename = available_filename(
            self.settings.MEDIA_ROOT,
            os.path.join(
                self.own_settings.archive_dl_folder,
                self.gallery.title + '.zip'))

        request_file = requests.get(
            self.gallery.archiver_key,
            stream='True',
            headers=self.settings.requests_headers,
            timeout=self.settings.timeout_timer,
            cookies=self.own_settings.cookies
        )

        filepath = os.path.join(self.settings.MEDIA_ROOT,
                                self.gallery.filename)
        with open(filepath, 'wb') as fo:
            for chunk in request_file.iter_content(4096):
                fo.write(chunk)

        self.gallery.filesize = get_zip_filesize(filepath)
        if self.gallery.filesize > 0:
            self.crc32 = calc_crc32(filepath)

            self.fileDownloaded = 1
            self.return_code = 1

        else:
            self.logger.error("Could not download archive")
            self.return_code = 0

    def update_archive_db(self, default_values: DataDict) -> Optional['Archive']:

        if not self.gallery:
            return None

        values = {
            'title': self.gallery.title,
            'title_jpn': self.gallery.title_jpn,
            'zipped': self.gallery.filename,
            'crc32': self.crc32,
            'filesize': self.gallery.filesize,
            'filecount': self.gallery.filecount,
        }
        default_values.update(values)
        return Archive.objects.update_or_create_by_values_and_gid(
            default_values,
            self.gallery.gid,
            zipped=self.gallery.filename
        )


API = (
    PandaBackupHttpFileDownloader,
)
