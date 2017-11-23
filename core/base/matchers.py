import os
import re

import time
from logging import Logger
from typing import Union, List

from core.base.comparison import get_gallery_closer_title_from_gallery_values, get_list_closer_gallery_titles_from_dict
from core.base.setup import Settings
from core.base.utilities import FakeLogger, GeneralUtils
from viewer.models import Gallery, Archive


class Meta(type):
    name = ''
    provider = ''

    def __str__(self):
        return "{}_{}".format(self.provider, self.name)


class Matcher(metaclass=Meta):

    name = ''
    provider = ''
    type = ''
    time_to_wait_after_compare = 0
    default_cutoff = 0.5

    def __str__(self):
        return "{}_{}".format(self.provider, self.name)

    def get_closer_match(self, file_path):

        title_to_search = self.format_to_search_title(file_path)

        if self.search_method(title_to_search):
            if self.time_to_wait_after_compare > 0:
                time.sleep(self.time_to_wait_after_compare)
            galleries_data = self.get_metadata_after_matching()
            galleries_data = [x for x in galleries_data if not self.general_utils.discard_by_tag_list(x['tags'])]
            if not galleries_data:
                return 0
            result = get_gallery_closer_title_from_gallery_values(
                self.format_to_compare_title(file_path), galleries_data, self.default_cutoff)
            if result.match_title == '':
                return 0
            self.match_link = result.match_link
            self.match_count = len(self.gallery_links)
            self.match_title = result.match_title
            self.match_values = result.match_values
            return 1
        else:
            return 0

    def create_closer_matches_values(self, title, cutoff=None, max_matches=20):

        if cutoff is None:
            cutoff = self.default_cutoff

        self.values_array = []
        results = []
        title_to_search = self.format_to_search_title(title)

        if self.search_method(title_to_search):
            if self.time_to_wait_after_compare > 0:
                time.sleep(self.time_to_wait_after_compare)
            galleries_data = self.get_metadata_after_matching()
            galleries_data = [x for x in galleries_data if not self.general_utils.discard_by_tag_list(x['tags'])]
            if galleries_data:
                self.values_array = galleries_data
                results = get_list_closer_gallery_titles_from_dict(
                    self.format_to_compare_title(title), self.values_array, cutoff, max_matches)
        return results

    def get_metadata_after_matching(self):
        return self.parser.fetch_multiple_gallery_data(self.gallery_links)

    def format_match_values(self):
        raise NotImplementedError

    def format_to_search_title(self, file_name):
        raise NotImplementedError

    def format_to_compare_title(self, file_name):
        raise NotImplementedError

    def search_method(self, title_to_search):
        raise NotImplementedError

    def update_archive(self):

        values = self.format_match_values()
        if self.settings.archive_reason:
            values['reason'] = self.settings.archive_reason
        Archive.objects.update_or_create_by_values_and_gid(
            values,
            self.match_gid,
            zipped=self.file_path
        )

    def update_gallery_db(self, values):

        check_exists = Gallery.objects.exists_by_gid(
            values['gid'])
        if check_exists and self.settings.replace_metadata:
            Gallery.objects.update_by_gid(values)
        elif not check_exists:
            Gallery.objects.add_from_values(values)

    def start_match(self, file_path, crc32):

        self.file_path = file_path
        # self.file_title = self.get_title_from_path(file_path)
        self.crc32 = crc32

        self.api_galleries = []

        self.return_code = self.get_closer_match(file_path)

        if self.return_code == 0:
            return False

        self.update_gallery_db(self.match_values)

        self.update_archive()
        return True

    @staticmethod
    def get_title_from_path(path):
        return re.sub('[_]', ' ', os.path.splitext(os.path.basename(path))[0])

    def __init__(self, settings: Settings, logger: Union[Logger, FakeLogger]) -> None:
        self.settings = settings
        self.own_settings = settings.providers[self.provider]
        if not logger:
            self.logger = logger
        else:
            self.logger = FakeLogger()
        self.general_utils = GeneralUtils(global_settings=settings)
        self.parser = self.settings.provider_context.get_parsers(self.settings, self.logger, filter_name=self.provider)[0]
        self.found_by = ''
        self.match_gid = None
        self.match_link = None
        self.values_array = []
        self.match_count = 0
        self.match_title = None
        self.api_galleries = []
        self.crc32 = None
        self.file_path = None
        # self.file_title = None
        self.return_code = None
        self.gallery_links: List[str] = []
        self.match_values = []
