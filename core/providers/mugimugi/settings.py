import typing

from core.base.types import ProviderSettings

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self) -> None:
        self.api_key = ''


def parse_config(global_settings: 'Settings', config: typing.Dict[str, typing.Any]) -> 'OwnSettings':

    settings = OwnSettings()
    if 'general' in config:
        if 'api_key' in config['general']:
            settings.api_key = config['general']['api_key']
    return settings
