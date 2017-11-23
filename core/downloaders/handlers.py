from core.base.setup import Settings
from core.base.utilities import OptionalLogger, GeneralUtils

from viewer.models import Gallery, Archive


class Meta(type):
    type = ''
    provider = ''

    def __str__(self):
        return "{}_{}".format(self.provider, self.type)


class BaseDownloader(metaclass=Meta):

    type = ''
    provider = ''
    archive_only = False

    def __str__(self):
        return "{}_{}".format(self.provider, self.type)

    def start_download(self):
        pass

    def update_archive_db(self, default_values):
        pass

    def update_gallery_db(self):
        if self.settings.keep_dl_type and 'dl_type' in self.original_gallery:
            del self.original_gallery['dl_type']
        self.gallery_db_entry = Gallery.objects.update_or_create_from_values(self.original_gallery)
        for archive in self.gallery_db_entry.archive_set.all():
            archive.title = archive.gallery.title
            archive.title_jpn = archive.gallery.title_jpn
            archive.simple_save()
            archive.tags.set(archive.gallery.tags.all())

    def init_download(self, gallery):

        self.original_gallery = gallery.copy()
        self.gallery = gallery

        self.original_gallery['dl_type'] = self.type
        self.start_download()

        if self.return_code == 0:
            return

        if not self.archive_only:
            self.update_gallery_db()

        if self.fileDownloaded == 1:

            default_values = {
                'match_type': self.type,
                'source_type': self.provider
            }

            if self.settings.archive_reason:
                default_values['reason'] = self.settings.archive_reason

            self.archive_db_entry = self.update_archive_db(default_values)

    def __init__(self, settings: Settings, logger: OptionalLogger, general_utils: GeneralUtils) -> None:
        self.settings = settings
        self.logger = logger
        self.general_utils = general_utils
        self.own_settings = settings.providers[self.provider]
        self.fileDownloaded = 0
        self.return_code = 0
        self.gallery_db_entry = None
        self.archive_db_entry = None
        self.crc32 = ''
        self.original_gallery = None
        self.gallery = None


class BaseInfoDownloader(BaseDownloader):

    type = 'info'

    def start_download(self):

        self.logger.info("Adding {} gallery, without downloading an archive".format(self.provider))

        self.return_code = 1


class BaseFakeDownloader(BaseDownloader):

    type = 'fake'

    def start_download(self):

        self.original_gallery['hidden'] = True

        self.logger.info("Adding {} gallery info to database, and faking the archive file".format(self.provider))

        self.fileDownloaded = 1
        self.return_code = 1

    def update_archive_db(self, default_values):

        values = {
            'zipped': '',
            'crc32': self.crc32,
        }
        if 'title' in self.gallery:
            values['title'] = self.gallery['title']
        if 'title_jpn' in self.gallery:
            values['title_jpn'] = self.gallery['title_jpn']
        if 'filesize' in self.gallery:
            values['filesize'] = self.gallery['filesize']
        if 'filecount' in self.gallery:
            values['filecount'] = self.gallery['filecount']
        default_values.update(values)
        return Archive.objects.create_by_values_and_gid(
            default_values,
            self.gallery['gid'],
        )
