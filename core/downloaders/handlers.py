import copy
import logging
import os
import shutil
import subprocess
import typing
from tempfile import mkdtemp
from typing import Optional, Any

from core.base.utilities import GeneralUtils, replace_illegal_name, get_base_filename_string_from_gallery_data, \
    available_filename, get_zip_fileinfo, calc_crc32
from core.base.types import GalleryData, TorrentClient, DataDict

if typing.TYPE_CHECKING:
    from core.base.setup import Settings
    from viewer.models import Gallery, Archive

logger = logging.getLogger(__name__)


class Meta(type):
    type = ''
    provider = ''

    def __str__(self) -> str:
        return "{}_{}".format(self.provider, self.type)


class BaseDownloader(metaclass=Meta):

    type = ''
    provider = ''
    archive_only = False
    no_metadata = False
    skip_if_hidden = False
    mark_hidden_if_last = False

    def __init__(self, settings: 'Settings', general_utils: GeneralUtils) -> None:
        self.settings = settings
        self.general_utils = general_utils
        self.own_settings = settings.providers[self.provider]
        self.fileDownloaded = 0
        self.return_code = 0
        self.gallery_db_entry: Optional['Gallery'] = None
        self.archive_db_entry: Optional['Archive'] = None
        self.crc32: str = ''
        self.original_gallery: Optional[GalleryData] = None
        self.gallery: Optional[GalleryData] = None

    def __str__(self) -> str:
        return "{}_{}".format(self.provider, self.type)

    def is_generic(self) -> bool:
        return self.provider == 'generic'

    def start_download(self) -> None:
        pass

    def update_archive_db(self, default_values: DataDict) -> Optional['Archive']:
        pass

    def update_gallery_db(self) -> None:
        if not self.original_gallery or not self.settings.gallery_model:
            return
        if self.settings.keep_dl_type and self.original_gallery.dl_type is not None:
            self.original_gallery.dl_type = None
        if self.type == 'submit':
            self.original_gallery.origin = self.settings.gallery_model.ORIGIN_SUBMITTED
        if self.no_metadata:
            self.original_gallery.status = self.settings.gallery_model.NO_METADATA
        if self.settings.gallery_reason:
            self.original_gallery.reason = self.settings.gallery_reason
        self.gallery_db_entry = self.settings.gallery_model.objects.update_or_create_from_values(self.original_gallery)
        if self.gallery_db_entry:
            for archive in self.gallery_db_entry.archive_set.all():
                if archive.gallery:
                    archive.title = archive.gallery.title
                    archive.title_jpn = archive.gallery.title_jpn
                    archive.simple_save()
                    archive.tags.set(archive.gallery.tags.all())

    def init_download(self, gallery: GalleryData) -> None:

        self.original_gallery = copy.deepcopy(gallery)
        self.gallery = gallery

        self.original_gallery.dl_type = self.type
        self.start_download()

        if self.return_code == 0:
            return

        if not self.archive_only:
            self.update_gallery_db()

        if self.fileDownloaded == 1:

            default_values: dict[str, Any] = {
                'match_type': self.type,
                'source_type': self.provider
            }

            if self.settings.archive_reason:
                default_values['reason'] = self.settings.archive_reason
            if self.settings.archive_details:
                default_values['details'] = self.settings.archive_details
            if self.settings.archive_source:
                default_values['source_type'] = self.settings.archive_source
            if self.settings.archive_user:
                default_values['user'] = self.settings.archive_user

            self.archive_db_entry = self.update_archive_db(default_values)


class BaseInfoDownloader(BaseDownloader):

    type = 'info'

    def start_download(self) -> None:

        logger.info("Adding {} gallery, without downloading an archive".format(self.provider))

        self.return_code = 1


class BaseFakeDownloader(BaseDownloader):

    type = 'fake'

    def start_download(self) -> None:

        if not self.original_gallery:
            return

        self.original_gallery.hidden = True

        logger.info("Adding {} gallery info to database, and faking the archive file".format(self.provider))

        self.fileDownloaded = 1
        self.return_code = 1

    def update_archive_db(self, default_values: DataDict) -> Optional['Archive']:

        if not self.gallery or not self.settings.archive_model:
            return None

        values: DataDict = {
            'zipped': '',
            'crc32': self.crc32,
        }
        if self.gallery.title is not None:
            values['title'] = self.gallery.title
        if self.gallery.title_jpn is not None:
            values['title_jpn'] = self.gallery.title_jpn
        if self.gallery.filesize is not None:
            values['filesize'] = self.gallery.filesize
        if self.gallery.filecount is not None:
            values['filecount'] = self.gallery.filecount
        default_values.update(values)
        return self.settings.archive_model.objects.create_by_values_and_gid(
            default_values,
            (self.gallery.gid, self.gallery.provider),
        )


class BaseTorrentDownloader(BaseDownloader):

    type = 'torrent'

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        self.expected_torrent_name = ''
        self.expected_torrent_extension = ''

    def connect_and_download(self, client: TorrentClient, torrent_link: str) -> None:
        if not self.gallery:
            return None
        client.connect()
        if client.send_url:
            result = client.add_url(
                torrent_link,
                download_dir=self.settings.torrent['download_dir']
            )
        else:
            result = client.add_torrent(
                self.general_utils.get_torrent(
                    torrent_link,
                    self.own_settings.cookies,
                    convert_to_base64=client.convert_to_base64
                ),
                download_dir=self.settings.torrent['download_dir']
            )
        if result:
            if client.expected_torrent_name:
                self.expected_torrent_name = "{} [{}]".format(
                    client.expected_torrent_name, self.gallery.gid
                )
            else:

                to_use_filename = get_base_filename_string_from_gallery_data(self.gallery)

                self.expected_torrent_name = "{} [{}]".format(
                    replace_illegal_name(to_use_filename), self.gallery.gid
                )
            if client.expected_torrent_extension:
                self.expected_torrent_extension = client.expected_torrent_extension
            else:
                self.expected_torrent_extension = ".zip"

            self.fileDownloaded = 1
            self.return_code = 1
            if client.total_size > 0:
                self.gallery.filesize = client.total_size
            self.gallery.filename = os.path.join(
                self.own_settings.torrent_dl_folder,
                replace_illegal_name(self.expected_torrent_name) + self.expected_torrent_extension
            )
            logger.info(
                "Torrent added, expecting downloaded name: {}, local name: {}".format(
                    self.expected_torrent_name,
                    self.gallery.filename
                )
            )
        else:
            self.return_code = 0
            logger.error("There was an error adding the torrent to the client")


class BaseGalleryDLDownloader(BaseDownloader):

    type = 'gallerydl'

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)

    def start_download(self) -> None:

        if not self.gallery or not self.gallery.link:
            return

        if self.settings.gallery_dl.executable_path:
            exe_path_to_use = shutil.which(self.settings.gallery_dl.executable_path)
        else:
            exe_path_to_use = shutil.which(self.settings.gallery_dl.executable_name)

        if not exe_path_to_use:
            self.return_code = 0
            logger.error("The gallery-dl executable was not found")
            return

        directory_path = mkdtemp()

        arguments = ["--zip", "--dest", "{}".format(
            directory_path
        )]

        if self.own_settings.proxy:
            arguments.append("--proxy")
            arguments.append("{}".format(self.own_settings.proxy))

        if self.settings.gallery_dl.config_file:
            arguments.append("--config")
            arguments.append("{}".format(self.settings.gallery_dl.config_file))

        if self.settings.gallery_dl.extra_arguments:
            arguments.append("{}".format(self.settings.gallery_dl.extra_arguments))

        arguments.append("{}".format(self.gallery.link))

        logger.info("Calling gallery-dl: {}.".format(" ".join([exe_path_to_use, *arguments])))

        process_result = subprocess.run(
            [exe_path_to_use, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        if process_result.stderr:
            self.return_code = 0
            logger.error("An error was captured when running gallery-dl: {}".format(process_result.stderr))
            return

        if process_result.returncode != 0:
            self.return_code = 0
            logger.error("Return code was not 0: {}".format(process_result.returncode))
            return

        # If we downloaded more than one file, get the latest one
        output_path = ''
        file_name = ''
        for (dir_path, dir_names, filenames) in os.walk(directory_path):
            for current_file in filenames:
                file_name = current_file
                output_path = os.path.join(dir_path, current_file)

        if not output_path:
            self.return_code = 0
            logger.error("The resulting download file was not found")
            return

        if not output_path or not os.path.isfile(output_path):
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

        filepath = os.path.join(self.settings.MEDIA_ROOT, self.gallery.filename)

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

        if not self.gallery or not self.settings.archive_model:
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
        return self.settings.archive_model.objects.update_or_create_by_values_and_gid(
            default_values,
            (self.gallery.gid, self.gallery.provider),
            zipped=self.gallery.filename
        )
