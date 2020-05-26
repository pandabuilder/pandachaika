import os
import typing

from core.base.types import ProviderSettings, DataDict

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self, global_settings: 'Settings', config: typing.Dict[str, typing.Any]) -> None:
        super().__init__(global_settings, config)
        self.torrent_dl_folder = ''


def parse_config(global_settings: 'Settings', config: typing.Dict[str, typing.Any]) -> 'OwnSettings':

    settings = OwnSettings(global_settings, config)

    if 'locations' in config:
        if 'torrent_dl_folder' in config['locations']:
            settings.torrent_dl_folder = config['locations']['torrent_dl_folder']
            if not os.path.exists(os.path.join(global_settings.MEDIA_ROOT, settings.torrent_dl_folder)):
                os.makedirs(os.path.join(global_settings.MEDIA_ROOT, settings.torrent_dl_folder))
        else:
            settings.torrent_dl_folder = global_settings.torrent_dl_folder
    return settings
