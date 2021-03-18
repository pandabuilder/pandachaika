import typing

from core.base.types import ProviderSettings

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self, global_settings: 'Settings', config: dict[str, typing.Any]) -> None:
        super().__init__(global_settings, config)
        self.api_key = ''
        # Automatically add this text to "unwanted_title" field on generated wanted galleries
        self.unwanted_title = ''


def parse_config(global_settings: 'Settings', config: dict[str, typing.Any]) -> 'OwnSettings':

    settings = OwnSettings(global_settings, config)

    if 'general' in config:
        if 'api_key' in config['general']:
            settings.api_key = config['general']['api_key']
    if 'wanted' in config:
        if 'unwanted_title' in config['wanted']:
            settings.unwanted_title = config['wanted']['unwanted_title']
    return settings
