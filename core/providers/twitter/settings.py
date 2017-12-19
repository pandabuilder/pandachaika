import typing

from core.base.types import ProviderSettings

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self) -> None:
        self.token = ''
        self.token_secret = ''
        self.consumer_key = ''
        self.consumer_secret = ''


def parse_config(global_settings: 'Settings', config: typing.Dict[str, typing.Any]) -> 'OwnSettings':

    settings = OwnSettings()
    if 'general' in config:
        if 'token' in config['general']:
            settings.token = config['general']['token']
        if 'token_secret' in config['general']:
            settings.token_secret = config['general']['token_secret']
        if 'consumer_key' in config['general']:
            settings.consumer_key = config['general']['consumer_key']
        if 'consumer_secret' in config['general']:
            settings.consumer_secret = config['general']['consumer_secret']
    return settings
