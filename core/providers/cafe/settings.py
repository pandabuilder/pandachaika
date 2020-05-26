import os
import typing
from typing import Dict

from core.base.types import ProviderSettings

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self, global_settings: 'Settings', config: typing.Dict[str, typing.Any]) -> None:
        super().__init__(global_settings, config)
        self.archive_dl_folder = ''


def parse_config(global_settings: 'Settings', config: Dict[str, typing.Any]) -> 'OwnSettings':

    settings = OwnSettings(global_settings, config)

    if 'locations' in config:
        if 'archive_dl_folder' in config['locations']:
            settings.archive_dl_folder = config['locations']['archive_dl_folder']
            if not os.path.exists(os.path.join(global_settings.MEDIA_ROOT, settings.archive_dl_folder)):
                os.makedirs(os.path.join(global_settings.MEDIA_ROOT, settings.archive_dl_folder))
        else:
            settings.archive_dl_folder = global_settings.archive_dl_folder
    return settings
