from core.base.utilities import get_scored_matches
from core.base.types import GalleryData


def get_list_closer_text_from_list[T](
    original: str, texts: list[tuple[str, T]], cutoff: float, max_matches: int
) -> list[tuple[str, T, float]] | None:

    compare_texts = [text[0] for text in texts]

    matches = get_scored_matches(original, compare_texts, max_matches, cutoff)

    if len(matches) == 0:
        return None

    results: list[tuple[str, T, float]] = []

    for i, compare_text in enumerate(compare_texts):
        for match in matches:
            if compare_text == match[1]:
                results.append((texts[i][0], texts[i][1], match[0]))
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
