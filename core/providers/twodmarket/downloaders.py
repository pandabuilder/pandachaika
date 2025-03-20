from core.downloaders.handlers import BaseInfoDownloader
from . import constants


class InfoDownloader(BaseInfoDownloader):

    provider = constants.provider_name


API = (InfoDownloader,)
