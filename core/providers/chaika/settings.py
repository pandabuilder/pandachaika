import os
import typing

from core.base.types import ProviderSettings

from . import constants

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self, global_settings: "Settings", config: dict[str, typing.Any]) -> None:
        super().__init__(global_settings, config)
        self.archive_dl_folder = ""
        self.url = constants.base_url
        self.metadata_url = constants.base_url
        self.feed_url = constants.feed_url


def parse_config(global_settings: "Settings", config: dict[str, typing.Any]) -> "OwnSettings":

    settings = OwnSettings(global_settings, config)

    if "general" in config:
        if "url" in config["general"]:
            settings.url = config["general"]["url"]
        if "metadata_url" in config["general"]:
            settings.metadata_url = config["general"]["metadata_url"]
        if "feed_url" in config["general"]:
            settings.feed_url = config["general"]["feed_url"]
    if "locations" in config:
        if "archive_dl_folder" in config["locations"]:
            settings.archive_dl_folder = config["locations"]["archive_dl_folder"]
        else:
            settings.archive_dl_folder = global_settings.archive_dl_folder
    return settings
