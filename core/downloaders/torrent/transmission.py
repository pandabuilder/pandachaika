from typing import Any, Union, Optional

import transmissionrpc
import os

from base64 import b64encode
from http.client import BadStatusLine
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPSHandler
import ssl


from transmissionrpc.error import HTTPHandlerError

from core.base.types import TorrentClient, DataDict


class Transmission(TorrentClient):

    name = 'transmission'
    convert_to_base64 = True
    send_url = False

    def __init__(self, address: str = 'localhost', port: int = 9091, user: str = '', password: str = '', secure: bool = True) -> None:
        super().__init__(address=address, port=port, user=user, password=password, secure=secure)
        self.trans = None
        self.error = ''

    def __str__(self) -> str:
        return self.name

    def add_url(self, enc_torrent: str, download_dir: Optional[str] = None) -> bool:
        result = self.add_torrent(enc_torrent, download_dir=download_dir)
        if self.expected_torrent_name and not self.expected_torrent_extension:
            self.expected_torrent_name = os.path.splitext(self.expected_torrent_name)[0]
        return result

    def add_torrent(self, enc_torrent: Union[str, bytes], download_dir: Optional[str] = None) -> bool:

        if not self.trans:
            return False
        self.total_size = 0

        if self.set_expected:
            self.expected_torrent_name = ''
            self.expected_torrent_extension = ''

        try:
            torr = self.trans.add_torrent(
                enc_torrent,
                download_dir=download_dir,
                timeout=25
            )

            if self.set_expected:
                self.expected_torrent_name = torr.name

            c = self.trans.get_files(torr.id)
            for file_t in c[torr.id]:
                self.total_size += int(c[torr.id][file_t]['size'])
                if self.set_expected and torr.name == c[torr.id][file_t]['name']:
                    name_split = os.path.splitext(self.expected_torrent_name)
                    self.expected_torrent_name = name_split[0]
                    self.expected_torrent_extension = name_split[1]

            return True

        except transmissionrpc.TransmissionError as e:
            self.error = e.message
            if 'invalid or corrupt torrent file' in e.message:
                return False
            elif 'duplicate torrent' in e.message:
                return True
            else:
                return False

    def connect(self) -> bool:
        try:
            if 'https://' in self.address:
                http_handler: Optional[TransmissionHTTPSHandler] = TransmissionHTTPSHandler(secure=self.secure)
            else:
                http_handler = None
            self.trans = transmissionrpc.Client(
                address=self.address,
                user=self.user,
                password=self.password,
                http_handler=http_handler
            )
        except transmissionrpc.TransmissionError as e:
            self.error = e.message
            return False
        return True


class TransmissionHTTPSHandler(transmissionrpc.HTTPHandler):

    """
    The default HTTPS handler provided with transmissionrpc.
    """

    def __init__(self, secure: bool = True) -> None:
        transmissionrpc.HTTPHandler.__init__(self)
        self.http_opener = build_opener()
        self.auth: DataDict = {}
        self.secure = secure

    def set_authentication(self, uri: str, login: str, password: str) -> None:
        if self.secure:
            context = ssl.create_default_context()
        else:
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
            context.verify_mode = ssl.CERT_NONE
            context.check_hostname = False
        self.http_opener = build_opener(HTTPSHandler(context=context))
        self.auth = {'Authorization': 'Basic %s' %
                     b64encode(str.encode(login + ":" + password)).decode('utf-8')}

    def request(self, url: str, query: str, headers: dict[Any, Any], timeout: int) -> str:
        headers = {**self.auth, **headers}
        request = Request(url, query.encode('utf-8'), headers)
        try:
            response = self.http_opener.open(request, timeout=timeout)
        except HTTPError as http_error:
            if http_error.fp is None:
                raise HTTPHandlerError(
                    http_error.filename, http_error.code, http_error.msg, dict(http_error.hdrs))  # type: ignore
            else:
                raise HTTPHandlerError(
                    http_error.filename, http_error.code, http_error.msg,  # type: ignore
                    dict(http_error.hdrs), http_error.read())  # type: ignore
        except URLError as url_error:
            # urllib2.URLError documentation is horrendous!
            # Try to get the tuple arguments of URLError
            if hasattr(url_error.reason, 'args') and isinstance(url_error.reason.args, tuple) and len(url_error.reason.args) == 2:
                raise HTTPHandlerError(
                    httpcode=url_error.reason.args[0], httpmsg=url_error.reason.args[1])
            else:
                raise HTTPHandlerError(
                    httpmsg='urllib2.URLError: %s' % url_error.reason)
        except BadStatusLine as line_error:
            raise HTTPHandlerError(
                httpmsg='httplib.BadStatusLine: %s' % line_error.line)  # type: ignore
        return response.read().decode('utf-8')
