import os
import typing

from core.base.types import ProviderSettings

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self, global_settings: 'Settings', config: dict[str, typing.Any]) -> None:
        super().__init__(global_settings, config)
        self.archive_dl_folder = ''
        self.megadl_executable_path: str = ''
        self.megadl_executable_name: str = 'megatools'
        self.extra_megadl_arguments: list[str] = []


def parse_config(global_settings: 'Settings', config: dict[str, typing.Any]) -> 'OwnSettings':

    settings = OwnSettings(global_settings, config)

    if 'general' in config:
        if 'megadl_executable_path' in config['general']:
            settings.megadl_executable_path = config['general']['megadl_executable_path']
        if 'megadl_executable_name' in config['general']:
            settings.megadl_executable_name = config['general']['megadl_executable_name']
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
