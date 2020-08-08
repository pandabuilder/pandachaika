from datetime import datetime
import typing
from typing import Optional, List, Union, Dict, Any, Tuple
if typing.TYPE_CHECKING:
    from core.base.setup import Settings


# gid and provider are mandatory, since they are a unique constrain on the database.
class GalleryData:
    def __init__(
            self, gid: str, provider: str,
            token: Optional[str] = None, link: Optional[str] = None,
            tags: List[str] = None, title: Optional[str] = None,
            title_jpn: Optional[str] = None, comment: Optional[str] = None, gallery_container_gid: Optional[str] = None,
            category: Optional[str] = None, posted: Optional[datetime] = None,
            filesize: Optional[int] = None, filecount: Optional[int] = None,
            expunged: Optional[int] = None, rating: Optional[str] = None,
            fjord: Optional[bool] = None, hidden: Optional[bool] = None,
            uploader: Optional[str] = None, thumbnail_url: Optional[str] = None,
            dl_type: Optional[str] = None, public: Optional[bool] = None,
            content: Optional[str] = None, archiver_key: Optional[str] = None,
            root: Optional[str] = None, filename: Optional[str] = None,
            queries: Optional[int] = None, thumbnail: Optional[str] = None,
            status: Optional[int] = None, origin: Optional[int] = None,
            reason: Optional[str] = None, **kwargs: Any
    ) -> None:
        self.gid = gid
        self.token = token
        self.link = link
        if tags:
            self.tags = tags
        else:
            self.tags = []
        self.gallery_container_gid = gallery_container_gid
        self.provider = provider
        self.title = title
        self.title_jpn = title_jpn
        self.comment = comment
        self.category = category
        self.posted = posted
        self.filesize = filesize
        self.filecount = filecount
        self.uploader = uploader
        self.thumbnail = thumbnail
        self.thumbnail_url = thumbnail_url
        self.dl_type = dl_type
        self.expunged = expunged
        self.rating = rating
        self.fjord = fjord
        self.hidden = hidden
        self.public = public
        self.status = status
        self.origin = origin
        self.reason = reason
        self.content = content
        self.archiver_key = archiver_key
        self.root = root
        self.filename = filename
        self.queries = queries
        self.extra_data: Dict = {}

    def __str__(self) -> str:
        return str(self.__dict__)

    def __repr__(self) -> str:
        return str(self.__dict__)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GalleryData):
            return False
        return self.__dict__ == other.__dict__


DataDict = Dict[str, Any]

MatchesValues = Tuple[str, GalleryData, float]


class ProviderSettings:
    def __init__(self, global_settings: 'Settings', config: Dict[str, Any]) -> None:
        self.cookies: DataDict = {}
        self.proxy: str = ""
        self.proxies: DataDict = {}
        self.timeout_timer: int = global_settings.timeout_timer
        self.autochecker_timer: float = global_settings.autochecker.cycle_timer
        self.autochecker_enable: bool = global_settings.autochecker.enable
        self.wait_timer: int = global_settings.wait_timer

        if 'cookies' in config:
            self.cookies.update(config['cookies'])
        if 'general' in config:
            if 'proxy' in config['general']:
                self.proxy = config['general']['proxy']
                self.proxies = {'http': config['general']['proxy'], 'https': config['general']['proxy']}
            if 'timeout_timer' in config['general']:
                self.timeout_timer = int(config['general']['timeout_timer'])
            if 'autochecker_timer' in config['general']:
                self.autochecker_timer = float(config['general']['autochecker_timer'])
            if 'autochecker_enable' in config['general']:
                self.autochecker_enable = config['general'].getboolean('autochecker_enable')
            if 'wait_timer' in config['general']:
                self.wait_timer = int(config['general']['wait_timer'])
        if 'proxies' in config:
            self.proxies.update(config['proxies'])


class TorrentClient(object):

    name = 'torrent'
    convert_to_base64 = False
    send_url = False
    type = 'torrent_handler'

    def __init__(self, address: str = 'localhost', port: int = 9091, user: str = '', password: str = '', secure: bool = True) -> None:
        self.address = address
        self.port = str(port)
        self.user = user
        self.password = password
        self.secure = secure
        self.total_size = 0
        self.expected_torrent_name = ''
        self.set_expected = True

    def add_torrent(self, torrent_data: Union[str, bytes], download_dir: str = None) -> bool:
        pass

    def add_url(self, url: str, download_dir: str = None) -> bool:
        pass

    def connect(self) -> bool:
        pass


QueueItem = Dict[str, Any]
