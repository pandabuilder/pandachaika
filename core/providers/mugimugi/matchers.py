import os

from core.base.matchers import Matcher
from core.base.utilities import (
    filecount_in_zip,
    get_zip_filesize,
    clean_title, request_with_retries)
from core.providers.mugimugi.utilities import convert_api_response_text_to_gallery_dicts
from . import constants


class TitleMatcher(Matcher):

    name = 'title'
    provider = constants.provider_name
    type = 'title'
    time_to_wait_after_compare = 0
    default_cutoff = 0.6

    def get_metadata_after_matching(self):
        return self.values_array

    def format_to_search_title(self, file_name):
        if file_name.endswith('.zip'):
            return clean_title(self.get_title_from_path(file_name))
        else:
            return clean_title(file_name)

    def format_to_compare_title(self, file_name):
        if file_name.endswith('.zip'):
            return clean_title(self.get_title_from_path(file_name))
        else:
            return clean_title(file_name)

    def search_method(self, title_to_search):
        return self.search_using_xml_api(title_to_search)

    def format_match_values(self):

        self.match_gid = self.match_values['gid']
        values = {
            'title': self.match_title,
            'title_jpn': self.match_values['title_jpn'],
            'zipped': self.file_path,
            'crc32': self.crc32,
            'match_type': self.found_by,
            'filesize': get_zip_filesize(os.path.join(self.settings.MEDIA_ROOT, self.file_path)),
            'filecount': filecount_in_zip(os.path.join(self.settings.MEDIA_ROOT, self.file_path)),
            'source_type': self.provider
        }

        return values

    def search_using_xml_api(self, title):

        if not self.own_settings.api_key:
            self.logger.error("Can't use {} API without an api key. Check {}/API_MANUAL.txt".format(
                self.name,
                constants.main_page
            ))
            return False

        page = 1
        galleries = []

        while True:
            link = '{}/api/{}/?S=objectSearch&sn={}&page={}'.format(
                constants.main_page,
                self.own_settings.api_key,
                title,
                page
            )

            response = request_with_retries(
                link,
                {
                    'headers': self.settings.requests_headers,
                    'timeout': self.settings.timeout_timer,
                },
                post=False,
                logger=self.logger
            )

            if not response:
                break

            response.encoding = 'utf-8'
            # Based on: https://www.doujinshi.org/API_MANUAL.txt

            api_galleries = convert_api_response_text_to_gallery_dicts(response.text)

            if not api_galleries:
                break

            galleries.extend(api_galleries)

            # API returns 25 max results per query, so if we get 24 or less, means there's no more pages.
            # API Manual says 25, but we get 50 results normally!
            if len(api_galleries) < 50:
                break

            page += 1

        self.values_array = galleries

        self.gallery_links = [x['link'] for x in galleries]
        if len(self.gallery_links) > 0:
            self.found_by = self.name
            return True
        else:
            return False


API = (
    TitleMatcher,
)
