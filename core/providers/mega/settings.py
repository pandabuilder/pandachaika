import os
import typing

from core.base.types import ProviderSettings

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self) -> None:
        self.archive_dl_folder = ''
        self.megadl_executable_path = ''
        self.megadl_executable_name = 'megadl'
        self.extra_megadl_arguments = []
        self.proxy = ''


def parse_config(global_settings: 'Settings', config: typing.Dict[str, typing.Any]) -> 'OwnSettings':

    settings = OwnSettings()

    if 'general' in config:
        if 'megadl_executable_path' in config['general']:
            settings.megadl_executable_path = config['general']['megadl_executable_path']
        if 'megadl_executable_name' in config['general']:
            settings.megadl_executable_name = config['general']['megadl_executable_name']
        if 'proxy' in config['general']:
            settings.proxy = config['general']['proxy']
        # | separated list
        if 'extra_megadl_arguments' in config['general']:
            settings.extra_megadl_arguments = config['general']['extra_megadl_arguments'].split("|")
    if 'locations' in config:
        if 'archive_dl_folder' in config['locations']:
            settings.archive_dl_folder = config['locations']['archive_dl_folder']
            if not os.path.exists(os.path.join(global_settings.MEDIA_ROOT, settings.archive_dl_folder)):
                os.makedirs(os.path.join(global_settings.MEDIA_ROOT, settings.archive_dl_folder))
        else:
            settings.archive_dl_folder = global_settings.archive_dl_folder
    return settings
