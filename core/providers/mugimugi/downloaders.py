from core.downloaders.handlers import BaseFakeDownloader, BaseInfoDownloader
from . import constants


class FakeDownloader(BaseFakeDownloader):

    provider = constants.provider_name


class InfoDownloader(BaseInfoDownloader):

    provider = constants.provider_name


API = (
    FakeDownloader,
    InfoDownloader,
)
