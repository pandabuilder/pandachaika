import typing

if typing.TYPE_CHECKING:
    from viewer.models import Archive


magazine_tags = {"other:anthology", "anthology"}
tankoubon_tags = {"other:tankoubon", "tankoubon"}
translated_tags = {"language:translated", "language:english"}
minimum_phash_to_match = 2

class ArchiveTagsComparer:
    def __init__(self, current_archive: 'Archive', compare_archive: 'Archive', current_tags: list[str], compare_tags: list[str], found_phash: list[str]):
        self.current_tags = current_tags
        self.current_tags_set = set(current_tags)
        self.compare_tags = compare_tags
        self.current_artists = [x for x in current_tags if x.startswith("artist:")]
        self.compare_tags_set = set(compare_tags)
        self.compare_artists = [x for x in compare_tags if x.startswith("artist:")]
        self.found_phash = found_phash
        self.compare_is_magazine = magazine_tags.intersection(self.compare_tags_set)
        self.compare_is_tankoubon = tankoubon_tags.intersection(self.compare_tags_set)
        self.current_has_one_artist = len(self.current_artists) == 1
        self.compare_has_one_artist = len(self.compare_artists) == 1
        self.compare_has_many_artists = len(self.compare_artists) > 1
        self.same_category = current_archive.get_es_category() == compare_archive.get_es_category()

    def translation_of_raw_anthology(self) -> bool:
        conditions = [
            self.current_is_translated(),
            self.compare_is_not_translated(),
            self.current_artist_is_on_compare_artists(),
            len(self.found_phash) > minimum_phash_to_match,
            self.compare_is_magazine or self.compare_is_tankoubon,
            self.same_category
        ]

        return all(conditions)

    def translation_of_raw_book(self) -> bool:
        conditions = [
            self.current_is_translated(),
            self.compare_is_not_translated(),
            self.same_one_artist(),
            len(self.found_phash) > minimum_phash_to_match,
            self.same_category
        ]

        return all(conditions)

    def same_one_artist(self):
        return self.current_has_one_artist and self.compare_has_one_artist and self.current_artists[0] in self.compare_artists[0]

    def current_artist_is_on_compare_artists(self):
        return self.current_has_one_artist and self.compare_has_many_artists and self.current_artists[0] in self.compare_artists

    def compare_is_not_translated(self):
        return not translated_tags.intersection(self.compare_tags_set)

    def current_is_translated(self):
        return translated_tags.intersection(self.current_tags_set)