import os
import typing
from typing import Dict

from core.base.types import ProviderSettings, DataDict

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self) -> None:
        self.cookies: DataDict = {}
        self.archive_dl_folder = ''


def parse_config(global_settings: 'Settings', config: Dict[str, typing.Any]) -> 'OwnSettings':

    settings = OwnSettings()

    if 'cookies' in config:
        settings.cookies.update(config['cookies'])
    if 'locations' in config:
        if 'archive_dl_folder' in config['locations']:
            settings.archive_dl_folder = config['locations']['archive_dl_folder']
            if not os.path.exists(os.path.join(global_settings.MEDIA_ROOT, settings.archive_dl_folder)):
                os.makedirs(os.path.join(global_settings.MEDIA_ROOT, settings.archive_dl_folder))
        else:
            settings.archive_dl_folder = global_settings.archive_dl_folder
    return settings
