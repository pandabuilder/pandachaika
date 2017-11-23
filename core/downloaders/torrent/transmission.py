import transmissionrpc
import os

from base64 import b64encode
from http.client import BadStatusLine
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPSHandler
import ssl


from transmissionrpc.error import HTTPHandlerError


class Transmission(object):

    name = 'transmission'
    convert_to_base64 = True
    send_url = False
    type = 'torrent_handler'

    def __str__(self):
        return self.name

    def add_url(self, enc_torrent, download_dir=None):
        result = self.add_torrent(enc_torrent, download_dir=download_dir)
        if self.expected_torrent_name:
            self.expected_torrent_name = os.path.splitext(self.expected_torrent_name)[0]
        return result

    def add_torrent(self, enc_torrent, download_dir=None):

        if not self.trans:
            return False
        self.total_size = 0
        self.expected_torrent_name = ''

        try:
            torr = self.trans.add_torrent(
                enc_torrent,
                download_dir=download_dir,
                timeout=25
            )

            self.expected_torrent_name = torr.name

            c = self.trans.get_files(torr.id)
            for file_t in c[torr.id]:
                self.total_size += int(c[torr.id][file_t]['size'])
                if torr.name == c[torr.id][file_t]['name']:
                    self.expected_torrent_name = os.path.splitext(
                        self.expected_torrent_name)[0]

            return True

        except transmissionrpc.TransmissionError as e:
            self.error = e.message
            if 'invalid or corrupt torrent file' in e.message:
                return False
            elif 'duplicate torrent' in e.message:
                return True
            else:
                return False

    def connect(self):
        try:
            if 'https://' in self.address:
                http_handler = TransmissionHTTPSHandler(secure=self.secure)
            else:
                http_handler = None
            self.trans = transmissionrpc.Client(
                address=self.address,
                user=self.user,
                password=self.password,
                http_handler=http_handler
            )
        except transmissionrpc.TransmissionError:
            return False
        return True

    def __init__(self, address='localhost', port=9091, user='', password='', secure=True):
        self.trans = None
        self.address = address
        self.port = str(port)
        self.user = user
        self.password = password
        self.secure = secure
        self.total_size = 0
        self.expected_torrent_name = ''
        self.error = ''


class TransmissionHTTPSHandler(transmissionrpc.HTTPHandler):

    """
    The default HTTPS handler provided with transmissionrpc.
    """

    def __init__(self, secure=True):
        transmissionrpc.HTTPHandler.__init__(self)
        self.http_opener = build_opener()
        self.auth = None
        self.secure = secure

    def set_authentication(self, uri, login, password):
        if self.secure:
            context = ssl.create_default_context()
        else:
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
            context.verify_mode = ssl.CERT_NONE
            context.check_hostname = False
        self.http_opener = build_opener(HTTPSHandler(context=context))
        self.auth = {'Authorization': 'Basic %s' %
                     b64encode(str.encode(login +
                                          ":" +
                                          password)).decode('utf-8')}

    def request(self, url, query, headers, timeout):
        headers = {**self.auth, **headers}
        request = Request(url, query.encode('utf-8'), headers)
        try:
            if (sys.version_info[0] == 2 and sys.version_info[1] > 5) or sys.version_info[0] > 2:
                response = self.http_opener.open(request, timeout=timeout)
            else:
                response = self.http_opener.open(request)
        except HTTPError as error:
            if error.fp is None:
                raise HTTPHandlerError(
                    error.filename, error.code, error.msg, dict(error.hdrs))
            else:
                raise HTTPHandlerError(
                    error.filename, error.code, error.msg,
                    dict(error.hdrs), error.read())
        except URLError as error:
            # urllib2.URLError documentation is horrendous!
            # Try to get the tuple arguments of URLError
            if hasattr(error.reason, 'args') and isinstance(error.reason.args, tuple) and len(error.reason.args) == 2:
                raise HTTPHandlerError(
                    httpcode=error.reason.args[0], httpmsg=error.reason.args[1])
            else:
                raise HTTPHandlerError(
                    httpmsg='urllib2.URLError: %s' % error.reason)
        except BadStatusLine as error:
            raise HTTPHandlerError(
                httpmsg='httplib.BadStatusLine: %s' % error.line)
        return response.read().decode('utf-8')
