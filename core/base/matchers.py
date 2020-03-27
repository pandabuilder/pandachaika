import os
import re

import time
from typing import List, Tuple, Optional
import typing

from core.base.comparison import get_gallery_closer_title_from_gallery_values, get_list_closer_gallery_titles_from_dict
from core.base.utilities import GeneralUtils
from core.base.types import GalleryData, OptionalLogger, FakeLogger, RealLogger, DataDict


if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class Meta(type):
    name = ''
    provider = ''

    def __str__(self) -> str:
        return "{}_{}".format(self.provider, self.name)


class Matcher(metaclass=Meta):

    name = ''
    provider = ''
    type = ''
    time_to_wait_after_compare = 0
    default_cutoff = 0.5

    def __init__(self, settings: 'Settings', logger: OptionalLogger) -> None:
        self.settings = settings
        self.own_settings = settings.providers[self.provider]
        if not logger:
            self.logger: RealLogger = FakeLogger()
        else:
            self.logger = logger
        self.general_utils = GeneralUtils(global_settings=settings)
        self.parser = self.settings.provider_context.get_parsers(self.settings, self.logger, filter_name=self.provider)[0]
        self.found_by = ''
        self.match_gid: Optional[str] = None
        self.match_link: Optional[str] = None
        self.values_array: List[GalleryData] = []
        self.match_count = 0
        self.match_title: Optional[str] = None
        self.api_galleries: List[GalleryData] = []
        self.crc32: Optional[str] = None
        self.file_path: str = ''
        # self.file_title = None
        self.return_code: int = 0
        self.gallery_links: List[str] = []
        self.match_values: Optional[GalleryData] = None

    def __str__(self) -> str:
        return "{}_{}".format(self.provider, self.name)

    def get_closer_match(self, file_path: str) -> int:

        title_to_search = self.format_to_search_title(file_path)

        if self.search_method(title_to_search):
            if self.time_to_wait_after_compare > 0:
                time.sleep(self.time_to_wait_after_compare)
            galleries_data = self.get_metadata_after_matching()
            if not galleries_data:
                return 0
            galleries_data = [x for x in galleries_data if not self.general_utils.discard_by_tag_list(x.tags)]
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

    def create_closer_matches_values(self, title: str, cutoff: Optional[float] = None, max_matches: int = 20) -> List[Tuple[str, GalleryData, float]]:

        if cutoff is None:
            cutoff = self.default_cutoff

        self.values_array = []
        results: List[Tuple[str, GalleryData, float]] = []
        title_to_search = self.format_to_search_title(title)

        if self.search_method(title_to_search):
            if self.time_to_wait_after_compare > 0:
                time.sleep(self.time_to_wait_after_compare)
            galleries_data = self.get_metadata_after_matching()
            if not galleries_data:
                return results
            galleries_data = [x for x in galleries_data if not self.general_utils.discard_by_tag_list(x.tags)]
            self.logger.info(
                "For matcher: {}, found {} results before filtering.".format(str(self), len(galleries_data))
            )
            if galleries_data:
                self.values_array = galleries_data
                results = get_list_closer_gallery_titles_from_dict(
                    self.format_to_compare_title(title), self.values_array, cutoff, max_matches)
        return results

    def get_metadata_after_matching(self) -> Optional[List[GalleryData]]:
        return self.parser.fetch_multiple_gallery_data(self.gallery_links)

    def format_match_values(self) -> Optional[DataDict]:
        raise NotImplementedError

    def format_to_search_title(self, file_name: str) -> str:
        raise NotImplementedError

    def format_to_compare_title(self, file_name: str) -> str:
        raise NotImplementedError

    def search_method(self, title_to_search: str) -> bool:
        raise NotImplementedError

    def update_archive(self) -> None:

        if not self.settings.archive_model:
            self.logger.error("Archive model has not been initiated.")
            return

        values = self.format_match_values()
        if values:
            if self.settings.archive_reason:
                values['reason'] = self.settings.archive_reason
            if self.settings.archive_details:
                values['details'] = self.settings.archive_details
            self.settings.archive_model.objects.update_or_create_by_values_and_gid(
                values,
                self.match_gid,
                zipped=self.file_path
            )

    def update_gallery_db(self, gallery_data: GalleryData) -> None:

        if not self.settings.gallery_model:
            self.logger.error("Gallery model has not been initiated.")
            return

        check_exists = self.settings.gallery_model.objects.exists_by_gid(
            gallery_data.gid)
        if check_exists and self.settings.replace_metadata:
            self.settings.gallery_model.objects.update_by_gid(gallery_data)
        elif not check_exists:
            self.settings.gallery_model.objects.add_from_values(gallery_data)

    def start_match(self, file_path: str, crc32: str) -> bool:

        self.file_path = file_path
        # self.file_title = self.get_title_from_path(file_path)
        self.crc32 = crc32

        self.api_galleries = []

        self.return_code = self.get_closer_match(file_path)

        if self.return_code == 0:
            return False

        if self.match_values:
            self.update_gallery_db(self.match_values)

        self.update_archive()
        return True

    @staticmethod
    def get_title_from_path(path: str) -> str:
        return re.sub('[_]', ' ', os.path.splitext(os.path.basename(path))[0])
