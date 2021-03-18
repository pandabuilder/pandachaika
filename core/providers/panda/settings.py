import os
import typing

from core.base.types import ProviderSettings

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self, global_settings: 'Settings', config: dict[str, typing.Any]) -> None:
        super().__init__(global_settings, config)
        self.hath_dl_folder = ''
        self.local_hath_folder = ''
        self.stop_page_number = 0
        self.remote_hath_dir = ''
        self.archive_dl_folder = ''
        self.torrent_dl_folder = ''
        self.accepted_rss_categories = (
            '[Doujinshi]',
            '[Manga]',
            '[Artist CG Sets]',
            '[Game CG Sets]',
            # '[Western]',
            # '[Image Sets]',
            '[Non-H]',
            # '[Cosplay]',
            # '[Asian Porn]',
            # '[Misc]',
            '[Private]'
        )


def parse_config(global_settings: 'Settings', config: dict[str, typing.Any]) -> 'OwnSettings':

    settings = OwnSettings(global_settings, config)

    settings.torrent_dl_folder = global_settings.torrent_dl_folder
    settings.archive_dl_folder = global_settings.archive_dl_folder

    if 'general' in config:
        if 'stop_page_number' in config['general']:
            settings.stop_page_number = int(config['general']['stop_page_number'])
        if 'accepted_rss_categories' in config['general']:
            settings.accepted_rss_categories = config['general']['accepted_rss_categories'].split(",")
    if 'locations' in config:
        if 'archive_dl_folder' in config['locations']:
            settings.archive_dl_folder = config['locations']['archive_dl_folder']
            if not os.path.exists(os.path.join(global_settings.MEDIA_ROOT, settings.archive_dl_folder)):
                os.makedirs(os.path.join(global_settings.MEDIA_ROOT, settings.archive_dl_folder))
        if 'torrent_dl_folder' in config['locations']:
            settings.torrent_dl_folder = config['locations']['torrent_dl_folder']
            if not os.path.exists(os.path.join(global_settings.MEDIA_ROOT, settings.torrent_dl_folder)):
                os.makedirs(os.path.join(global_settings.MEDIA_ROOT, settings.torrent_dl_folder))
        if 'hath_dl_folder' in config['locations']:
            settings.hath_dl_folder = config['locations']['hath_dl_folder']
            if not os.path.exists(os.path.join(global_settings.MEDIA_ROOT, settings.hath_dl_folder)):
                os.makedirs(os.path.join(global_settings.MEDIA_ROOT, settings.hath_dl_folder))
        if 'local_hath_folder' in config['locations']:
            settings.local_hath_folder = config['locations']['local_hath_folder']
        if 'remote_hath_dir' in config['locations']:
            settings.remote_hath_dir = config['locations']['remote_hath_dir']
    return settings
