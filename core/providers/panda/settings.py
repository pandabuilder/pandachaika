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
        self.remote_hath_dir = ''
        self.archive_dl_folder = ''
        self.torrent_dl_folder = ''
        self.accepted_rss_categories = (
            # '[Doujinshi]',
            # '[Manga]',
            # '[Artist CG Sets]',
            # '[Game CG Sets]',
            # '[Western]',
            # '[Image Sets]',
            # '[Non-H]',
            # '[Cosplay]',
            # '[Asian Porn]',
            # '[Misc]',
            # '[Private]'
        )
        self.use_ex_for_fjord = False
        self.auto_process_newer = False
        self.auto_process_first = False
        self.auto_process_parent = False
        self.mark_relationships = False
        self.api_concurrent_limit = 4  # Wiki says between 4-5
        self.api_wait_limit = 5.5  # Wiki says 5 seconds


def parse_config(global_settings: 'Settings', config: dict[str, typing.Any]) -> 'OwnSettings':

    settings = OwnSettings(global_settings, config)

    settings.torrent_dl_folder = global_settings.torrent_dl_folder
    settings.archive_dl_folder = global_settings.archive_dl_folder

    if 'general' in config:
        if 'accepted_rss_categories' in config['general']:
            settings.accepted_rss_categories = config['general']['accepted_rss_categories']
        if 'use_ex_for_fjord' in config['general']:
            settings.use_ex_for_fjord = config['general']['use_ex_for_fjord']
        if 'auto_process_newer' in config['general']:
            settings.auto_process_newer = config['general']['auto_process_newer']
        if 'auto_process_first' in config['general']:
            settings.auto_process_first = config['general']['auto_process_first']
        if 'auto_process_parent' in config['general']:
            settings.auto_process_parent = config['general']['auto_process_parent']
        # TODO: Mark galleries when a new gallery in chain is detected
        if 'mark_relationships' in config['general']:
            settings.mark_relationships = config['general']['mark_relationships']
    if 'api' in config:
        if 'concurrent_limit' in config['api']:
            settings.api_concurrent_limit = config['api']['concurrent_limit']
        if 'wait_limit' in config['api']:
            settings.api_wait_limit = config['api']['wait_limit']
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
