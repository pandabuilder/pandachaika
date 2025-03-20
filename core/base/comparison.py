import difflib
from typing import Optional

from core.base.utilities import get_scored_matches
from core.base.types import GalleryData


def get_closer_gallery_title_from_list(
    original: str, titles: list[tuple[str, str]], cutoff: float
) -> Optional[tuple[str, str]]:

    compare_titles = []
    compare_ids = []

    for title in titles:
        compare_titles.append(title[0])
        compare_ids.append((title[0], title[1]))

    matches = difflib.get_close_matches(original, list(compare_titles), 1, cutoff)
    if len(matches) == 0:
        return None
    match_title = matches[0]

    for i, compare_title in enumerate(compare_titles):
        if compare_title == match_title:
            return compare_ids[i][0], compare_ids[i][1]

    return None


def get_list_closer_gallery_titles_from_list(
    original: str, titles: list[tuple[str, str]], cutoff: float, max_matches: int
) -> Optional[list[tuple[str, str, float]]]:

    compare_titles = []
    compare_ids = []

    for title in titles:
        compare_titles.append(title[0])
        compare_ids.append([title[0], title[1]])

    matches = get_scored_matches(original, compare_titles, max_matches, cutoff)

    if len(matches) == 0:
        return None

    results = []

    for i, compare_title in enumerate(compare_titles):
        for match in matches:
            if compare_title == match[1]:
                results.append((compare_ids[i][0], compare_ids[i][1], match[0]))
                break

    return results


# Returns: list(gallery_title, gallery_dict, match_score)
def get_list_closer_gallery_titles_from_dict(
    original: str, gallery_datas: list[GalleryData], cutoff: float, max_matches: int
) -> list[tuple[str, GalleryData, float]]:

    compare_titles = []
    compare_ids = []
    results: list[tuple[str, GalleryData, float]] = []

    for gallery_data in gallery_datas:
        if gallery_data.title:
            compare_titles.append(gallery_data.title)
            compare_ids.append((gallery_data.title, gallery_data))
        if gallery_data.title_jpn:
            compare_titles.append(gallery_data.title_jpn)
            compare_ids.append((gallery_data.title_jpn, gallery_data))

    matches = get_scored_matches(original, compare_titles, max_matches, cutoff)

    if len(matches) == 0:
        return results

    for i, gallery_title in enumerate(compare_titles):
        for match in matches:
            if gallery_title == match[1]:
                results.append((compare_ids[i][0], compare_ids[i][1], match[0]))
                break

    return results


class ResultContainer:
    def __init__(self) -> None:
        self.match_title: Optional[str] = ""
        self.match_link: Optional[str] = ""
        self.match_values: Optional[GalleryData] = None


def get_gallery_closer_title_from_gallery_values(
    original: str, gallery_datas: list[GalleryData], cutoff: float
) -> ResultContainer:

    result = ResultContainer()
    compare_titles = []
    original_index = {}

    for i, gallery_dict in enumerate(gallery_datas):
        if gallery_dict.title:
            compare_titles.append(gallery_dict.title)
            original_index[len(compare_titles) - 1] = i

    matches = difflib.get_close_matches(original, compare_titles, 1, cutoff)
    if len(matches) == 0:
        return result
    result.match_title = str(matches[0])

    for i, compare_title in enumerate(compare_titles):
        if compare_title == result.match_title:
            result.match_values = gallery_datas[original_index[i]]
            result.match_link = gallery_datas[original_index[i]].link
            gallery_datas[original_index[i]].link = None

    return result
