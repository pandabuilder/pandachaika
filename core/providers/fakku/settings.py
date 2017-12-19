import typing

from core.base.types import ProviderSettings, DataDict

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self) -> None:
        self.cookies: DataDict = {}


def parse_config(global_settings: 'Settings', config: typing.Dict[str, typing.Any]) -> 'OwnSettings':

    settings = OwnSettings()

    if 'cookies' in config:
        settings.cookies.update(config['cookies'])
    return settings
