import os
import typing

from core.base.types import ProviderSettings

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self, global_settings: "Settings", config: dict[str, typing.Any]) -> None:
        super().__init__(global_settings, config)
        self.torrent_dl_folder = ""
        self.archive_dl_folder = ""


def parse_config(global_settings: "Settings", config: dict[str, typing.Any]) -> "OwnSettings":

    settings = OwnSettings(global_settings, config)

    settings.torrent_dl_folder = global_settings.torrent_dl_folder
    settings.archive_dl_folder = global_settings.archive_dl_folder

    if "locations" in config:
        if "torrent_dl_folder" in config["locations"]:
            settings.torrent_dl_folder = config["locations"]["torrent_dl_folder"]
        if "archive_dl_folder" in config["locations"]:
            settings.archive_dl_folder = config["locations"]["archive_dl_folder"]
    return settings
