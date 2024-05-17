import re
from tempfile import NamedTemporaryFile
from typing import Union, Optional

import requests
from requests.exceptions import RequestException
from requests.auth import HTTPBasicAuth

from core.base.types import TorrentClient


class uTorrent(TorrentClient):

    name = 'utorrent'
    convert_to_base64 = False
    send_url = False

    def __init__(self, address: str = 'localhost', port: int = 8080, user: str = '', password: str = '', secure: bool = True) -> None:
        super().__init__(address=address, port=port, user=user, password=password, secure=secure)
        self.auth: Optional[HTTPBasicAuth] = None
        self.token = ''
        self.UTORRENT_URL = ''

    def __str__(self) -> str:
        return self.name

    def add_torrent(self, torrent_data: Union[str, bytes], download_dir: Optional[str] = None) -> tuple[bool, Optional[str]]:

        self.total_size = 0
        self.expected_torrent_name = ''
        self.expected_torrent_extension = ''
        lf = NamedTemporaryFile()
        if isinstance(torrent_data, bytes):
            lf.write(torrent_data)
        else:
            lf.write(str.encode(torrent_data))

        params = {'action': 'add-file', 'token': self.token}
        files = {'torrent_file': open(lf.name, 'rb')}
        try:
            response = requests.post(
                self.UTORRENT_URL,
                auth=self.auth,
                params=params,
                files=files,
                timeout=25).json()
            lf.close()
            if 'error' in response:
                return False, None
            else:
                return True, None
        except RequestException:
            lf.close()
            return False, None

    def add_url(self, url: str, download_dir: Optional[str] = None) -> tuple[bool, Optional[str]]:

        self.total_size = 0
        self.expected_torrent_name = ''
        self.expected_torrent_extension = ''

        params = {'action': 'add-url', 'token': self.token, 's': url}
        try:
            response = requests.get(
                self.UTORRENT_URL,
                auth=self.auth,
                # cookies=self.cookies,
                params=params,
                timeout=25).json()
            if 'error' in response:
                return False, None
            else:
                return True, None
        except RequestException:
            return False, None

    def connect(self) -> bool:
        self.UTORRENT_URL = '%s:%s/gui/' % (self.address, self.port)
        UTORRENT_URL_TOKEN = '%stoken.html' % self.UTORRENT_URL
        REGEX_UTORRENT_TOKEN = r'<div[^>]*id=[\"\']token[\"\'][^>]*>([^<]*)</div>'
        self.auth = HTTPBasicAuth(self.user, self.password)
        try:
            r = requests.get(UTORRENT_URL_TOKEN, auth=self.auth, timeout=25)
            result = re.search(REGEX_UTORRENT_TOKEN, r.text)
            if result:
                self.token = result.group(1)
            else:
                return False
            return True
        except requests.exceptions.RequestException:
            return False
        # guid = r.cookies['GUID']
        # self.cookies = dict(GUID=guid)

    def get_download_progress(self, download_list: list[tuple[str, TorrentClient.TorrentKey]]) -> list[tuple[TorrentClient.TorrentKey, float]]:
        results: list[tuple[TorrentClient.TorrentKey, float]] = []

        params = {'action': 'list', 'token': self.token}
        try:
            response = requests.get(
                self.UTORRENT_URL,
                auth=self.auth,
                # cookies=self.cookies,
                params=params,
                timeout=25).json()
            if 'error' in response:
                return results
        except RequestException:
            return results

        torrent_progress = {x[0]: x[4]*1000 for x in response['torrents']}

        results = [(x[1], torrent_progress[int(x[0])]) for x in download_list if int(x[0]) in torrent_progress]

        return results
