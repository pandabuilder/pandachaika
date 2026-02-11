from collections import defaultdict

from django.test import TestCase

from core.base.setup import Settings
from core.base.types import GalleryData
from core.providers.panda.parsers import Parser as PandaParser
from viewer.models import Gallery, WantedGallery, Tag

class WantedGalleryElasticSearchTest(TestCase):
    def setUp(self):
        # Tags
        english_tag = Tag.objects.create(scope="language", name="english")
        artist_tag = Tag.objects.create(scope="artist", name="suzunomoku")
        artist2_tag = Tag.objects.create(scope="artist", name="mitsuya")

        # Galleries
        self.test_gallery1 = Gallery.objects.create(title="sample non public gallery 1", gid="344", provider="panda")
        self.test_gallery2 = Gallery.objects.create(title="sample non public gallery 2", gid="342", provider="test")

        # WantedGalleries
        self.test_wanted_gallery1 = WantedGallery.objects.create(
            title="test wanted gallery",
            book_type="Manga",
            publisher="wanimagazine",
            reason="wanimagazine",
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            match_expression='tags.full:"artist:suzunomoku"'
        )

        self.test_wanted_gallery3 = WantedGallery.objects.create(
            title="test wanted gallery 3",
            publisher="wanimagazine",
            reason="wanimagazine",
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            found=True,
            match_expression='provider:panda AND tags.full:"artist:suzunomoku"'
        )

        self.test_gallery4 = Gallery.objects.create(
            title="New Release 4", gid="34665", provider="panda", category="Manga", token="4324239"
        )
        self.test_gallery4.tags.set([english_tag, artist_tag, artist2_tag])

    def test_match_gallery(self) -> None:
        settings = Settings(load_from_disk=True)
        parser = PandaParser(settings)

        wanted_galleries = WantedGallery.objects.all()

        gallery_link = "https://e-hentai.org/g/2079628/cec767079f/"

        incoming_gallery = GalleryData(
            "2079628",
            "panda",
            token="cec767079f",
            link=gallery_link,
            title="[Suzunomoku] Dopyu-Dopyu Of The Dead (WEEKLY Kairakuten 2021 No. 45) [English]",
            title_jpn="ドピュードピュ・オブ・ザ・デッド",
            thumbnail_url="https://ehgt.org/64/ff/64ff16f7d4ddde36be517a498a50a88911ca68de-4149126-1359-1920-png_l.jpg",
            filecount=17,
            filesize=64185347,
            category="Manga",
            uploader="GeeseIsLeese",
            tags=[
                "artist:suzunomoku",
                "parody:original",
                "female:ahegao",
                "language:english",
                "language:translated",
            ],
        )

        gallery_wanted_lists: dict[str, list["WantedGallery"]] = defaultdict(list)

        # 5 viewer related queries, 1 log related query
        if settings.disable_sql_log:
            expected_queries = 5
        else:
            expected_queries = 6
        with self.assertNumQueries(expected_queries):
            parser.compare_gallery_with_wanted_filters(
                incoming_gallery, gallery_link, wanted_galleries, gallery_wanted_lists
            )

        self.assertEqual(len(gallery_wanted_lists[incoming_gallery.gid]), 2)
        self.assertEqual(WantedGallery.objects.filter(found=True).count(), 2)

        incoming_gallery1 = self.test_gallery1.as_gallery_data()
        incoming_gallery4 = self.test_gallery4.as_gallery_data()

        gallery_wanted_lists = defaultdict(list)

        # 2 are not already found, remaining it is.
        parser.compare_gallery_with_wanted_filters(
            incoming_gallery1, incoming_gallery1.link, wanted_galleries, gallery_wanted_lists
        )

        parser.compare_gallery_with_wanted_filters(
            incoming_gallery4, incoming_gallery4.link, wanted_galleries, gallery_wanted_lists
        )

        self.assertEqual(len(gallery_wanted_lists[incoming_gallery1.gid]), 0)
        self.assertEqual(len(gallery_wanted_lists[incoming_gallery4.gid]), 2)

        rematch_result_1 = self.test_gallery1.match_against_wanted_galleries(wanted_galleries, skip_already_found=False)
        rematch_result_4 = self.test_gallery4.match_against_wanted_galleries(wanted_galleries, skip_already_found=False)

        # Make sure compare_gallery_with_wanted_filters gives the same result as match_against_wanted_galleries
        self.assertEqual(rematch_result_1, gallery_wanted_lists[incoming_gallery1.gid])
        self.assertEqual(rematch_result_4, gallery_wanted_lists[incoming_gallery4.gid])
