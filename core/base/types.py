from dataclasses import dataclass
from datetime import datetime
import typing
from typing import Optional, Union, Any
if typing.TYPE_CHECKING:
    from core.base.setup import Settings


# gid and provider are mandatory, since they are a unique constraint on the database.
class GalleryData:
    def __init__(
            self, gid: str, provider: str,
            token: Optional[str] = None, link: Optional[str] = None,
            tags: Optional[list[str]] = None, title: Optional[str] = None,
            title_jpn: Optional[str] = None, comment: Optional[str] = None,
            gallery_container_gid: Optional[str] = None, gallery_contains_gids: Optional[list[str]] = None,
            magazine_gid: Optional[str] = None, magazine_chapters_gids: Optional[list[str]] = None,
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
            first_gallery_gid: Optional[str] = None,
            parent_gallery_gid: Optional[str] = None,
            extra_provider_data: Optional[list[tuple[str, str, Any]]] = None,
            provider_metadata: Optional[str] = None,
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
        self.gallery_contains_gids = gallery_contains_gids
        self.magazine_gid = magazine_gid
        self.magazine_chapters_gids = magazine_chapters_gids
        self.first_gallery_gid = first_gallery_gid
        self.parent_gallery_gid = parent_gallery_gid
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
        self.extra_data: dict = {}
        self.extra_provider_data = extra_provider_data
        self.provider_metadata = provider_metadata

    def __str__(self) -> str:
        return str(self.__dict__)

    def __repr__(self) -> str:
        return str(self.__dict__)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GalleryData):
            return False
        return self.__dict__ == other.__dict__


DataDict = dict[str, Any]

MatchesValues = tuple[str, GalleryData, float]


class ProviderSettings:
    def __init__(self, global_settings: 'Settings', config: dict[str, Any]) -> None:
        self.cookies: DataDict = {}
        self.proxy: str = ""
        self.proxies: DataDict = {}
        self.timeout_timer: int = global_settings.timeout_timer
        self.wait_timer: int = global_settings.wait_timer
        self.stop_page_number: Optional[int] = None

        # Auto updater
        self.autoupdater_enable: bool = global_settings.autoupdater.enable
        self.autoupdater_timer: float = global_settings.autoupdater.cycle_timer
        self.autoupdater_buffer_back: int = global_settings.autoupdater.buffer_back
        self.autoupdater_buffer_after: int = global_settings.autoupdater.buffer_after

        if 'cookies' in config:
            self.cookies.update(config['cookies'])
        if 'general' in config:
            if 'proxy' in config['general']:
                self.proxy = config['general']['proxy']
                self.proxies = {'http': config['general']['proxy'], 'https': config['general']['proxy']}
            if 'timeout_timer' in config['general']:
                self.timeout_timer = int(config['general']['timeout_timer'])
            if 'wait_timer' in config['general']:
                self.wait_timer = int(config['general']['wait_timer'])
            if 'autoupdater_timer' in config['general']:
                self.autoupdater_timer = float(config['general']['autoupdater_timer'])
            if 'autoupdater_enable' in config['general']:
                self.autoupdater_enable = config['general']['autoupdater_enable']
            if 'autoupdater_buffer_back' in config['general']:
                self.autoupdater_buffer_back = int(config['general']['autoupdater_buffer_back'])
            if 'autoupdater_buffer_after' in config['general']:
                self.autoupdater_buffer_after = int(config['general']['autoupdater_buffer_after'])
            if 'stop_page_number' in config['general']:
                self.stop_page_number = int(config['general']['stop_page_number'])
        if 'proxies' in config:
            self.proxies.update(config['proxies'])


class TorrentClient:

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
        self.expected_torrent_extension = ''
        self.set_expected = True
        self.error = ''

    def add_torrent(self, torrent_data: Union[str, bytes], download_dir: Optional[str] = None) -> bool:
        return False

    def add_url(self, url: str, download_dir: Optional[str] = None) -> bool:
        return False

    def connect(self) -> bool:
        return False


QueueItem = dict[str, Any]


@dataclass
class ArchiveGenericFile:
    file_name: str
    file_size: float = 0
    position: int = 1
