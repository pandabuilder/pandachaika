import json
import os
import shutil
import re
from tempfile import mkdtemp
from zipfile import ZipFile

import requests
from bs4 import BeautifulSoup

from core.base.utilities import calc_crc32, get_zip_filesize
from core.downloaders.handlers import BaseDownloader, BaseInfoDownloader
from .utilities import guess_gallery_read_url
from viewer.models import Archive
from core.base.utilities import (available_filename,
                                 replace_illegal_name)
from . import constants


class ArchiveDownloader(BaseDownloader):

    type = 'archive'
    provider = constants.provider_name

    def start_download(self):

        self.gallery['title'] = replace_illegal_name(
            self.gallery['title'])
        self.gallery['filename'] = available_filename(
            self.settings.MEDIA_ROOT,
            os.path.join(
                self.own_settings.archive_dl_folder,
                self.gallery['title'] + '.zip'))
        if self.gallery['content']:
            soup_1 = BeautifulSoup(self.gallery['content'], 'html.parser')
        else:
            gallery_page = requests.get(
                self.gallery['link'],
                headers=self.settings.requests_headers,
                timeout=self.settings.timeout_timer
            )
            soup_1 = BeautifulSoup(gallery_page.content, 'html.parser')

        gallery_read = soup_1.find("a", {"class": "x-btn-rounded"})['href']

        # Some URLs are really bad formatted
        gallery_read = re.sub(
            r'.*(' + re.escape(constants.main_page) + r'/manga/read/.+/0/1/).*', r'\1',
            gallery_read,
            flags=re.DOTALL
        )

        if not gallery_read or gallery_read in constants.bad_urls or not gallery_read.startswith(constants.main_page):
            self.logger.warning("Reading gallery page not available, trying to guess the name.")
            gallery_read = guess_gallery_read_url(self.gallery['link'], self.gallery)

        if not gallery_read.endswith('page/1'):
            gallery_read += 'page/1'

        page_regex = re.compile(r"(.*?page/)(\d+)/*$", re.IGNORECASE)

        last_image = ''

        directory_path = mkdtemp()

        self.logger.info('Downloading gallery: {}'.format(self.gallery['title']))

        second_pass = False
        while True:

            try:
                gallery_read_page = requests.get(
                    gallery_read,
                    headers=self.settings.requests_headers,
                    timeout=self.settings.timeout_timer
                )
            except requests.exceptions.MissingSchema:
                self.logger.error("Malformed URL: {}, skipping".format(gallery_read))
                self.return_code = 0
                shutil.rmtree(directory_path, ignore_errors=True)
                return

            if gallery_read_page.status_code == 404:
                if gallery_read.endswith('page/1'):
                    if not second_pass:
                        gallery_read = guess_gallery_read_url(self.gallery['link'], self.gallery, False)
                        second_pass = True
                        continue
                    self.logger.error("Last page was the first one: {}, stopping".format(gallery_read))
                    self.return_code = 0
                    shutil.rmtree(directory_path, ignore_errors=True)
                    return
                # yield("Got to last gallery page, stopping")
                break

            soup_2 = BeautifulSoup(gallery_read_page.content, 'html.parser')
            img_find = soup_2.find("img", {"class": "open"})

            if not img_find:
                self.logger.error("Gallery not available, skipping")
                self.return_code = 0
                shutil.rmtree(directory_path, ignore_errors=True)
                return

            img = img_find['src']

            if last_image != '' and last_image == img:
                # yield('Current image is the same as previous, skipping')
                break
            last_image = img
            img_name = os.path.basename(img)

            request_file = requests.get(
                img,
                headers=self.settings.requests_headers,
                timeout=self.settings.timeout_timer
            )
            if request_file.status_code == 404:
                # yield("Got to last image, stopping")
                break
            with open(os.path.join(directory_path, img_name), "wb") as fo:
                for chunk in request_file.iter_content(4096):
                    fo.write(chunk)

            page_match = page_regex.search(gallery_read)

            if page_match:
                gallery_read = page_match.group(1) + str(int(page_match.group(2)) + 1)
            else:
                # yield("Could not match to change page, stopping")
                break

        file_path = os.path.join(
            self.settings.MEDIA_ROOT,
            self.gallery['filename']
        )

        self.gallery['filecount'] = 0

        with ZipFile(file_path, 'w') as archive:
            for (root_path, _, file_names) in os.walk(directory_path):
                for current_file in file_names:
                    self.gallery['filecount'] += 1
                    archive.write(
                        os.path.join(root_path, current_file), arcname=os.path.basename(current_file))
        shutil.rmtree(directory_path, ignore_errors=True)

        self.gallery['filesize'] = get_zip_filesize(file_path)
        if self.gallery['filesize'] > 0:
            self.crc32 = calc_crc32(file_path)
            self.fileDownloaded = 1
            self.return_code = 1

    def update_archive_db(self, default_values):

        values = {
            'title': self.gallery['title'],
            'title_jpn': '',
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


class ArchiveJSDownloader(BaseDownloader):

    type = 'archive_js'
    provider = constants.provider_name

    @staticmethod
    def get_img_urls_from_gallery_read_page(content):
        soup = BeautifulSoup(content, 'html.parser')
        script_content = soup.find("script", type="text/javascript")

        if script_content:
            m = re.search(r'var pages = (.*?);\n', script_content.get_text())
            if m:
                urls = json.loads(m.group(1))
                return [x['url'] for x in urls]
        return []

    def start_download(self):

        self.gallery['title'] = replace_illegal_name(
            self.gallery['title'])
        self.gallery['filename'] = available_filename(
            self.settings.MEDIA_ROOT,
            os.path.join(
                self.own_settings.archive_dl_folder,
                self.gallery['title'] + '.zip'))
        if self.gallery['content']:
            soup_1 = BeautifulSoup(self.gallery['content'], 'html.parser')
        else:
            gallery_page = requests.get(
                self.gallery['link'],
                headers=self.settings.requests_headers,
                timeout=self.settings.timeout_timer
            )
            soup_1 = BeautifulSoup(gallery_page.content, 'html.parser')

        gallery_read = soup_1.find("a", {"class": "x-btn-rounded"})['href']

        # Some URLs are really bad formatted
        gallery_read = re.sub(
            r'.*(' + re.escape(constants.main_page) + r'/manga/read/.+/0/1/).*', r'\1',
            gallery_read,
            flags=re.DOTALL
        )

        if not gallery_read or gallery_read in constants.bad_urls or not gallery_read.startswith(constants.main_page):
            self.logger.warning("Reading gallery page not available, trying to guess the name.")
            gallery_read = guess_gallery_read_url(self.gallery['link'], self.gallery)

        if not gallery_read.endswith('page/1'):
            gallery_read += 'page/1'

        self.logger.info('Downloading gallery: {}'.format(self.gallery['title']))

        try:
            gallery_read_page = requests.get(
                gallery_read,
                headers=self.settings.requests_headers,
                timeout=self.settings.timeout_timer
            )
        except requests.exceptions.MissingSchema:
            self.logger.error("Malformed URL: {}, skipping".format(gallery_read))
            self.return_code = 0
            return

        if gallery_read_page.status_code != 200:
            gallery_read = guess_gallery_read_url(self.gallery['link'], self.gallery, False)
            try:
                gallery_read_page = requests.get(
                    gallery_read,
                    headers=self.settings.requests_headers,
                    timeout=self.settings.timeout_timer
                )
            except requests.exceptions.MissingSchema:
                self.logger.error("Malformed URL: {}, skipping".format(gallery_read))
                self.return_code = 0
                return

        if gallery_read_page.status_code == 200:

            image_urls = self.get_img_urls_from_gallery_read_page(gallery_read_page.content)

            if not image_urls:
                self.logger.error("Could not find image links, archive not downloaded")
                self.return_code = 0
                return

            directory_path = mkdtemp()

            for image_url in image_urls:
                img_name = os.path.basename(image_url)

                request_file = requests.get(
                    image_url,
                    headers=self.settings.requests_headers,
                    timeout=self.settings.timeout_timer
                )
                if request_file.status_code == 404:
                    self.logger.warning("Image link reported 404 error, stopping")
                    break
                with open(os.path.join(directory_path, img_name), "wb") as fo:
                    for chunk in request_file.iter_content(4096):
                        fo.write(chunk)

            file_path = os.path.join(
                self.settings.MEDIA_ROOT,
                self.gallery['filename']
            )

            self.gallery['filecount'] = 0

            with ZipFile(file_path, 'w') as archive:
                for (root_path, _, file_names) in os.walk(directory_path):
                    for current_file in file_names:
                        self.gallery['filecount'] += 1
                        archive.write(
                            os.path.join(root_path, current_file), arcname=os.path.basename(current_file))
            shutil.rmtree(directory_path, ignore_errors=True)

            self.gallery['filesize'] = get_zip_filesize(file_path)
            if self.gallery['filesize'] > 0:
                self.crc32 = calc_crc32(file_path)
                self.fileDownloaded = 1
                self.return_code = 1
        else:
            self.logger.error("Wrong HTML code returned, could not download, link: {}".format(gallery_read))
            self.return_code = 0

    def update_archive_db(self, default_values):

        values = {
            'title': self.gallery['title'],
            'title_jpn': '',
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


API = (
    ArchiveDownloader,
    ArchiveJSDownloader,
    InfoDownloader,
)
