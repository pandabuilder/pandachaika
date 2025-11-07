import typing

from core.base.types import ProviderSettings

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class OwnSettings(ProviderSettings):
    def __init__(self, global_settings: "Settings", config: dict[str, typing.Any]) -> None:
        super().__init__(global_settings, config)
        self.token = ""
        self.token_secret = ""
        self.consumer_key = ""
        self.consumer_secret = ""
        self.add_as_public = False
        # Automatically add this text to "unwanted_title" field on generated wanted galleries
        self.unwanted_title = ""
        self.regexp_unwanted_title = ""
        self.regexp_unwanted_title_icase = ""
        self.enabled_handles: list[str] = []


def parse_config(global_settings: "Settings", config: dict[str, typing.Any]) -> "OwnSettings":

    settings = OwnSettings(global_settings, config)
    if "general" in config:
        if "token" in config["general"]:
            settings.token = config["general"]["token"]
        if "token_secret" in config["general"]:
            settings.token_secret = config["general"]["token_secret"]
        if "consumer_key" in config["general"]:
            settings.consumer_key = config["general"]["consumer_key"]
        if "consumer_secret" in config["general"]:
            settings.consumer_secret = config["general"]["consumer_secret"]
    if "wanted" in config:
        if "add_as_public" in config["wanted"]:
            settings.add_as_public = config["wanted"]["add_as_public"]
        if "unwanted_title" in config["wanted"]:
            settings.unwanted_title = config["wanted"]["unwanted_title"]
        if "regexp_unwanted_title" in config["wanted"]:
            settings.regexp_unwanted_title = config["wanted"]["regexp_unwanted_title"]
        if "regexp_unwanted_title_icase" in config["wanted"]:
            settings.regexp_unwanted_title_icase = config["wanted"]["regexp_unwanted_title_icase"]
    if "enabled_handles" in config:
        settings.enabled_handles = config["enabled_handles"]
    return settings
