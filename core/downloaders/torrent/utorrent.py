import re
from tempfile import NamedTemporaryFile

import requests
from requests.exceptions import RequestException
from requests.auth import HTTPBasicAuth


class uTorrent(object):

    name = 'utorrent'
    convert_to_base64 = False
    send_url = False
    type = 'torrent_handler'

    def __str__(self):
        return self.name

    def add_torrent(self, torrent_data, download_dir=None):

        self.total_size = 0
        self.expected_torrent_name = ''
        lf = NamedTemporaryFile()
        lf.write(torrent_data)

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
                return False
            else:
                return True
        except RequestException:
            lf.close()
            return False

    def add_url(self, url, download_dir=None):

        self.total_size = 0
        self.expected_torrent_name = ''

        params = {'action': 'add-url', 'token': self.token, 's': url}
        try:
            response = requests.get(
                self.UTORRENT_URL,
                auth=self.auth,
                # cookies=self.cookies,
                params=params,
                timeout=25).json()
            if 'error' in response:
                return False
            else:
                return True
        except RequestException:
            return False

    def connect(self):
        self.UTORRENT_URL = '%s:%s/gui/' % (self.address, self.port)
        UTORRENT_URL_TOKEN = '%stoken.html' % self.UTORRENT_URL
        REGEX_UTORRENT_TOKEN = r'<div[^>]*id=[\"\']token[\"\'][^>]*>([^<]*)</div>'
        self.auth = HTTPBasicAuth(self.user, self.password)
        r = requests.get(UTORRENT_URL_TOKEN, auth=self.auth, timeout=25)
        self.token = re.search(REGEX_UTORRENT_TOKEN, r.text).group(1)
        # guid = r.cookies['GUID']
        # self.cookies = dict(GUID=guid)

    def __init__(self, address='localhost', port=8080, user='', password='', secure=True):
        self.address = address
        self.port = str(port)
        self.user = user
        self.password = password
        self.total_size = 0
        self.expected_torrent_name = ''
        self.auth = None
        self.token = None
        self.UTORRENT_URL = ''
