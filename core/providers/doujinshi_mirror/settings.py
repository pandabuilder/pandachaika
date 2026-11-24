import typing

from core.base.types import ProviderSettings

from . import constants

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self, global_settings: "Settings", config: dict[str, typing.Any]) -> None:
        super().__init__(global_settings, config)
        # Base URL of the doujinshi-browser server, e.g. https://doujinshi.example.com/. Empty: provider disabled.
        self.url = ""
        if "wait_timer" not in (config.get("general") or {}):
            self.wait_timer = constants.default_wait_timer


def parse_config(global_settings: "Settings", config: dict[str, typing.Any]) -> "OwnSettings":

    settings = OwnSettings(global_settings, config)

    general = config.get("general") or {}
    if general.get("url"):
        settings.url = str(general["url"]).strip()

    return settings
