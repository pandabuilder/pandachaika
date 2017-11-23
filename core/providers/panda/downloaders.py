import os
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from core.base.utilities import calc_crc32, get_zip_filesize, request_with_retries
from core.downloaders.handlers import BaseDownloader, BaseInfoDownloader, BaseFakeDownloader
from core.downloaders.torrent import get_torrent_client
from core.providers.panda.utilities import ArchiveHTMLParser, TorrentHTMLParser
from viewer.models import Archive
from core.base.utilities import (available_filename,
                                 replace_illegal_name)
from . import constants


# We define a skip_if_hidden parameter in here,
# since there's no pint in trying to use those downloaders in hidden galleries.
class ArchiveDownloader(BaseDownloader):

    type = 'archive'
    provider = constants.provider_name
    skip_if_hidden = True

    def request_archive_download(self, root, gid, token, key):

        url = root + '/archiver.php'

        params = {'gid': gid, 'token': token, 'or': key}

        response = request_with_retries(
            url,
            {
                'params': params,
                'cookies': self.own_settings.cookies,
                'data': constants.archive_download_data,
                'headers': self.settings.requests_headers,
                'timeout': self.settings.timeout_timer
            },
            post=True,
            logger=self.logger
        )

        return response

    def start_download(self):

        self.gallery['title'] = replace_illegal_name(
            self.gallery['title'])
        self.gallery['filename'] = available_filename(
            self.settings.MEDIA_ROOT,
            os.path.join(
                self.own_settings.archive_dl_folder,
                self.gallery['title'] + '.zip'))

        r = self.request_archive_download(
            self.gallery['root'],
            self.gallery['gid'],
            self.gallery['token'],
            self.gallery['archiver_key']
        )

        if not r:
            self.logger.error('ERROR: could not get download link.')
            self.return_code = 0

        archive_page_parser = ArchiveHTMLParser()
        archive_page_parser.feed(r.text)

        if 'Invalid archiver key' in r.text:
            self.logger.error("Invalid archiver key received.")
            self.return_code = 0
        else:
            if archive_page_parser.archive == '':
                self.logger.error('Could not find archive link, page text: {}'.format(r.text))
                self.return_code = 0
            else:
                m = re.match("(.*?)(\?.*?)", archive_page_parser.archive)
                if m:
                    archive_page_parser.archive = m.group(1)

                self.logger.info('Got link: {}, from url: {}'.format(archive_page_parser.archive, r.url))

                request_file = requests.get(
                    archive_page_parser.archive + '?start=1',
                    stream='True',
                    headers=self.settings.requests_headers,
                    timeout=self.settings.timeout_timer
                )

                if r.status_code == 200:
                    self.logger.info('Downloading gallery: {}.zip'.format(self.gallery['title']))
                    filepath = os.path.join(self.settings.MEDIA_ROOT,
                                            self.gallery['filename'])
                    with open(filepath, 'wb') as fo:
                        for chunk in request_file.iter_content(4096):
                            fo.write(chunk)

                    self.gallery['filesize'] = get_zip_filesize(filepath)
                    if self.gallery['filesize'] > 0:
                        self.crc32 = calc_crc32(filepath)

                        self.fileDownloaded = 1
                        self.return_code = 1

                else:
                    self.logger.error("Could not download archive")
                    self.return_code = 0

    def update_archive_db(self, default_values):

        values = {
            'title': self.gallery['title'],
            'title_jpn': self.gallery['title_jpn'],
            'zipped': self.gallery['filename'],
            'crc32': self.crc32,
            'filesize': self.gallery['filesize'],
            'filecount': self.gallery['filecount'],
        }
        default_values.update(values)
        return Archive.objects.update_or_create_by_values_and_gid(
            default_values,
            self.gallery['gid'],
            zipped=self.gallery['filename']
        )


class TorrentDownloader(BaseDownloader):

    type = 'torrent'
    provider = constants.provider_name
    skip_if_hidden = True

    def request_torrent_download(self, root, gid, token):

        url = root + '/gallerytorrents.php'

        params = {'gid': gid, 't': token}

        r = requests.get(
            url,
            params=params,
            cookies=self.own_settings.cookies,
            headers=self.settings.requests_headers,
            timeout=self.settings.timeout_timer
        )

        return r

    @staticmethod
    def validate_torrent(torrent_link, seeds, posted_date, gallery_posted_date):
        validated = True
        reasons = []
        if not torrent_link:
            validated = False
            reasons.append("Invalid URL.")
        else:
            if seeds <= 0:
                validated = False
                reasons.append("Less than 1 seed.")
            if not posted_date:
                validated = False
                reasons.append('Did not get a correct posted time.')
            else:
                parsed_posted_date = datetime.strptime(posted_date, '%Y-%m-%d %H:%M %z'),
                if parsed_posted_date < gallery_posted_date:
                    validated = False
                    reasons.append("Posted before gallery posted time.")
        return validated, reasons

    def start_download(self):
        client = get_torrent_client(self.settings.torrent)
        if not client:
            self.return_code = 0
            self.logger.error("No torrent client was found")
            return

        r = self.request_torrent_download(
            self.gallery['root'], self.gallery['gid'], self.gallery['token'])
        torrent_page_parser = TorrentHTMLParser()
        torrent_page_parser.feed(r.text)

        torrent_link = torrent_page_parser.torrent

        validated, reasons = self.validate_torrent(
            torrent_link,
            torrent_page_parser.seeds,
            torrent_page_parser.posted_date,
            self.gallery['posted']
        )

        if not validated:
            self.logger.error(
                "Torrent for gallery: {} for did not pass validation, reasons: {}"
                ", skipping.".format(self.gallery['link'], " ".join(reasons))
            )
            self.return_code = 0
            return

        m = re.match("(.*?)(\?p=\d+)", torrent_link)
        if m:
            torrent_link = m.group(1)

        self.logger.info("Adding torrent to client, seeds: {}".format(torrent_page_parser.seeds))
        client.connect()
        if client.send_url:
            result = client.add_url(
                torrent_link,
                download_dir=self.settings.torrent['download_dir']
            )
        else:
            result = client.add_torrent(
                self.general_utils.get_torrent(
                    torrent_link,
                    self.own_settings.cookies,
                    convert_to_base64=client.convert_to_base64
                ),
                download_dir=self.settings.torrent['download_dir']
            )
        if result:
            if client.expected_torrent_name:
                self.expected_torrent_name = "{} [{}]".format(client.expected_torrent_name, self.gallery['gid'])
            else:
                self.expected_torrent_name = "{} [{}]".format(
                    replace_illegal_name(self.gallery['title']), self.gallery['gid']
                )
            self.fileDownloaded = 1
            self.return_code = 1
            if client.total_size > 0:
                self.gallery['filesize'] = client.total_size
            self.gallery['filename'] = os.path.join(
                self.own_settings.torrent_dl_folder,
                replace_illegal_name(self.expected_torrent_name) + '.zip'
            )
        else:
            self.return_code = 0
            self.logger.error("There was an error adding the torrent to the client")

    def update_archive_db(self, default_values):

        values = {
            'title': self.gallery['title'],
            'title_jpn': self.gallery['title_jpn'],
            'zipped': self.gallery['filename'],
            'crc32': self.crc32,
            'filesize': self.gallery['filesize'],
            'filecount': self.gallery['filecount'],
        }
        default_values.update(values)
        return Archive.objects.update_or_create_by_values_and_gid(
            default_values,
            self.gallery['gid'],
            zipped=self.gallery['filename']
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.expected_torrent_name = ''


class HathDownloader(BaseDownloader):

    type = 'hath'
    provider = constants.provider_name
    skip_if_hidden = True

    def start_download(self):

        r = self.request_hath_download(
            self.gallery['root'],
            self.gallery['gid'],
            self.gallery['token'],
            self.gallery['archiver_key']
        )

        if r.status_code == 200:

            r.encoding = 'utf-8'
            soup = BeautifulSoup(r.content, 'html.parser')

            container = soup.find(
                text=re.compile('An original resolution download has been queued for client')
            )

            if not container:
                self.logger.error("Could not find expected text in response.")
                self.return_code = 0
                return
            client_id = container.parent.find('strong')
            if client_id:
                self.logger.info("Queued download to client: {}".format(client_id.get_text()))

            self.gallery['filename'] = available_filename(
                self.settings.MEDIA_ROOT,
                os.path.join(
                    self.own_settings.hath_dl_folder,
                    replace_illegal_name(
                        self.gallery['title'] + " [" + str(self.gallery['gid']) + "]") + '.zip'
                )
            )

            self.fileDownloaded = 1
            self.return_code = 1
        else:
            self.logger.error('Did not get 200 response.')
            self.return_code = 0

    def request_hath_download(self, root, gid, token, key):

        url = root + '/archiver.php'

        params = {'gid': gid, 'token': token, 'or': key}

        for retry_count in range(3):
            try:
                r = requests.post(
                    url,
                    params=params,
                    cookies=self.own_settings.cookies,
                    data={'hathdl_xres': 'org'},
                    headers=self.settings.requests_headers,
                    timeout=self.settings.timeout_timer
                )
                return r
            except requests.exceptions.Timeout:
                if retry_count < 2:
                    self.logger.warning("Request failed, retrying: {}".format(retry_count))
                    continue
                else:
                    return None

    def update_archive_db(self, default_values):

        values = {
            'title': self.gallery['title'],
            'title_jpn': self.gallery['title_jpn'],
            'zipped': self.gallery['filename'],
            'crc32': self.crc32,
            'filesize': self.gallery['filesize'],
            'filecount': self.gallery['filecount'],
        }
        default_values.update(values)
        return Archive.objects.update_or_create_by_values_and_gid(
            default_values,
            self.gallery['gid'],
            zipped=self.gallery['filename']
        )


class InfoDownloader(BaseInfoDownloader):

    provider = constants.provider_name


class FakeDownloader(BaseFakeDownloader):

    provider = constants.provider_name


class UrlSubmitDownloader(BaseDownloader):

    type = 'submit'
    provider = constants.provider_name
    skip_if_hidden = False

    def start_download(self):

        if self.settings.force_dl_type:
            self.original_gallery['dl_type'] = "{}:{}".format(self.type, self.settings.force_dl_type)

        self.logger.info("Adding gallery submission info to database")

        self.return_code = 1


API = (
    ArchiveDownloader,
    TorrentDownloader,
    HathDownloader,
    InfoDownloader,
    FakeDownloader,
    UrlSubmitDownloader,
)
