import typing

from core.base.types import ProviderSettings

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self, global_settings: "Settings", config: dict[str, typing.Any]) -> None:
        super().__init__(global_settings, config)
        self.get_posted_date_from_feed = False
        self.get_posted_date_from_wayback = False


def parse_config(global_settings: "Settings", config: dict[str, typing.Any]) -> "OwnSettings":

    settings = OwnSettings(global_settings, config)

    if "general" in config:
        if "get_posted_date_from_feed" in config["general"]:
            settings.get_posted_date_from_feed = config["general"]["get_posted_date_from_feed"]
        if "get_posted_date_from_wayback" in config["general"]:
            settings.get_posted_date_from_wayback = config["general"]["get_posted_date_from_wayback"]

    return settings
