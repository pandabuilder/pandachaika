import difflib
from typing import List, Optional

from core.base.utilities import get_scored_matches


def get_closer_gallery_title_from_list(original: str, titles: List[List[str]], cutoff: float) -> Optional[List[str]]:

    compare_titles = []
    compare_ids = []

    for title in titles:
        compare_titles.append(title[0])
        compare_ids.append([title[0], title[1]])

    matches = difflib.get_close_matches(original, list(compare_titles), 1, cutoff)
    if len(matches) == 0:
        return None
    match_title = matches[0]

    for i, compare_title in enumerate(compare_titles):
        if compare_title == match_title:
            return [compare_ids[i][0], compare_ids[i][1]]

    return None


def get_list_closer_gallery_titles_from_list(original, titles, cutoff, max_matches):

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
                results.append(
                    (compare_ids[i][0], compare_ids[i][1], match[0]))
                break

    return results


# Returns: list(gallery_title, gallery_dict, match_score)
def get_list_closer_gallery_titles_from_dict(original, gallery_dicts, cutoff, max_matches):

    compare_titles = []
    compare_ids = []
    results = []

    for gallery_dict in gallery_dicts:
        compare_titles.append(gallery_dict['title'])
        compare_ids.append([gallery_dict['title'], gallery_dict])

    matches = get_scored_matches(original, compare_titles, max_matches, cutoff)

    if len(matches) == 0:
        return results

    for i, gallery_title in enumerate(compare_titles):
        for match in matches:
            if gallery_title == match[1]:
                results.append(
                    (compare_ids[i][0], compare_ids[i][1], match[0]))
                break

    return results


class ResultContainer:
    def __init__(self):
        self.match_title = ''
        self.match_link = ''
        self.match_values = {}


def get_gallery_closer_title_from_gallery_values(original, gallery_dicts, cutoff):

    result = ResultContainer()
    compare_titles = []

    for gallery_dict in gallery_dicts:
        compare_titles.append(gallery_dict['title'])

    matches = difflib.get_close_matches(
        original, compare_titles, 1, cutoff)
    if len(matches) == 0:
        return result
    result.match_title = matches[0]

    for i, compare_title in enumerate(compare_titles):
        if compare_title == result.match_title:
            result.match_values = gallery_dicts[i]
            result.match_link = gallery_dicts[i]['link']
            del gallery_dicts[i]['link']

    return result
