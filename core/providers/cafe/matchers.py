import os
from typing import Any, Dict

from core.base.matchers import Matcher
from core.base.types import DataDict
from core.base.utilities import filecount_in_zip, get_zip_filesize, request_with_retries
from . import constants
from .utilities import clean_title


class TitleMatcher(Matcher):

    name = 'title'
    provider = constants.provider_name
    type = 'title'
    time_to_wait_after_compare = 0
    default_cutoff = 0.6

    def format_to_search_title(self, file_name: str) -> str:
        if file_name.endswith('.zip'):
            return clean_title(self.get_title_from_path(file_name))
        else:
            return clean_title(file_name)

    def format_to_compare_title(self, file_name: str) -> str:
        if file_name.endswith('.zip'):
            return self.get_title_from_path(file_name)
        else:
            return file_name

    def search_method(self, title_to_search: str) -> bool:
        return self.compare_by_title(title_to_search)

    def format_match_values(self) -> DataDict:

        self.match_gid = self.match_values.gid
        values = {
            'title': self.match_title,
            'title_jpn': '',
            'zipped': self.file_path,
            'crc32': self.crc32,
            'match_type': self.found_by,
            'filesize': get_zip_filesize(os.path.join(self.settings.MEDIA_ROOT, self.file_path)),
            'filecount': filecount_in_zip(os.path.join(self.settings.MEDIA_ROOT, self.file_path)),
            'source_type': self.provider
        }

        return values

    def compare_by_title(self, title: str) -> bool:

        headers = {'Content-Type': 'application/json'}

        api_link = constants.posts_api_url
        payload = {'search': title}

        response = request_with_retries(
            api_link,
            {
                'headers': {**headers, **self.settings.requests_headers},
                'timeout': self.settings.timeout_timer,
                'params': payload
            },
            post=False,
            logger=self.logger
        )

        if not response:
            return False
        response.encoding = 'utf-8'
        try:
            response_data = response.json()
        except(ValueError, KeyError):
            return False

        matches_links = set()

        for gallery in response_data:
            matches_links.add(gallery['link'])

        self.gallery_links = list(matches_links)
        if len(self.gallery_links) > 0:
            self.found_by = self.name
            return True
        else:
            return False


API = (
    TitleMatcher,
)
