from collections import defaultdict

from django.test import TestCase

from core.base.setup import Settings
from core.base.types import GalleryData
from core.base.comparison import get_list_closer_gallery_titles_from_list
from core.providers.panda.parsers import Parser as PandaParser
from viewer.models import Gallery, WantedGallery, Tag, FoundGallery


class CoreTest(TestCase):
    def setUp(self):
        # Galleries
        self.test_gallery1 = Gallery.objects.create(title='sample non public gallery 1', gid='344', provider='panda')
        self.test_gallery2 = Gallery.objects.create(title='sample non public gallery 2', gid='342', provider='test')

    def test_repeated_archives(self):

        title_to_check = 'my title'
        galleries_title_id = [(gallery.title, gallery.pk) for gallery in Gallery.objects.all()]
        cutoff = 0.4
        max_matches = 10

        similar_list = get_list_closer_gallery_titles_from_list(
            title_to_check, galleries_title_id, cutoff, max_matches)

        self.assertIsNone(similar_list)

        title_to_check_2 = 'public gallery 1'

        similar_list = get_list_closer_gallery_titles_from_list(
            title_to_check_2, galleries_title_id, cutoff, max_matches)

        self.assertIsNotNone(similar_list)
        self.assertEqual(len(similar_list), 2)
        self.assertEqual(similar_list[0][0], 'sample non public gallery 1')
        self.assertEqual(similar_list[0][2], 0.7441860465116279)


class WantedGalleryTest(TestCase):
    def setUp(self):
        # Tags
        english_tag = Tag.objects.create(scope="language", name="english")
        artist_tag = Tag.objects.create(scope="artist", name="suzunomoku")

        # Galleries
        self.test_gallery1 = Gallery.objects.create(title='sample non public gallery 1', gid='344', provider='panda')
        self.test_gallery2 = Gallery.objects.create(title='sample non public gallery 2', gid='342', provider='test')

        # WantedGalleries
        self.test_wanted_gallery1 = WantedGallery.objects.create(
            title='test wanted gallery',
            book_type='Manga',
            publisher='wanimagazine',
            reason='wanimagazine',
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
        )
        self.test_wanted_gallery1.wanted_tags.set([english_tag, artist_tag])

        for i in range(10):
            WantedGallery.objects.create(
                title='repeated wanted gallery {}'.format(i),
                search_title='Dopyu',
                book_type='Manga',
                publisher='wanimagazine',
                reason='wanimagazine',
                should_search=True,
                keep_searching=True,
                notify_when_found=False,
            )

        self.test_wanted_gallery3 = WantedGallery.objects.create(
            title='test wanted gallery 3',
            search_title='gallery',
            publisher='wanimagazine',
            reason='wanimagazine',
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            found=True,
        )

        self.test_gallery3 = Gallery.objects.create(
            title='existing gallery', gid='345', provider='panda', category='Manga',
            token='4324234'
        )

        FoundGallery.objects.get_or_create(
            wanted_gallery=self.test_wanted_gallery3,
            gallery=self.test_gallery3
        )

    def test_match_gallery(self):
        settings = Settings(load_from_disk=True)
        parser = PandaParser(settings)

        wanted_galleries = WantedGallery.objects.all()

        gallery_link = 'https://e-hentai.org/g/2079628/cec767079f/'

        incoming_gallery = GalleryData(
            '2079628',
            'panda',
            token='cec767079f',
            link=gallery_link,
            title='[Suzunomoku] Dopyu-Dopyu Of The Dead (WEEKLY Kairakuten 2021 No. 45) [English]',
            title_jpn='ドピュードピュ・オブ・ザ・デッド',
            thumbnail_url='https://ehgt.org/64/ff/64ff16f7d4ddde36be517a498a50a88911ca68de-4149126-1359-1920-png_l.jpg',
            filecount=17,
            filesize=64185347,
            category='Manga',
            uploader='GeeseIsLeese',
            tags=[
                'artist:suzunomoku',
                'parody:original',
                'female:ahegao',
                'language:english',
                'language:translated',
            ],
        )

        gallery_wanted_lists: dict[str, list['WantedGallery']] = defaultdict(list)

        with self.assertNumQueries(7):
            parser.compare_gallery_with_wanted_filters(incoming_gallery, gallery_link, wanted_galleries, gallery_wanted_lists)

        self.assertEqual(len(gallery_wanted_lists[incoming_gallery.gid]), 11)
        self.assertEqual(WantedGallery.objects.filter(found=True).count(), 12)

        incoming_gallery2 = self.test_gallery3.as_gallery_data()
        incoming_gallery3 = self.test_gallery1.as_gallery_data()
        incoming_gallery4 = self.test_gallery2.as_gallery_data()

        gallery_wanted_lists: dict[str, list['WantedGallery']] = defaultdict(list)

        # 2 are not already found, remaining it is.
        parser.compare_gallery_with_wanted_filters(incoming_gallery2, incoming_gallery2.link, wanted_galleries, gallery_wanted_lists)
        parser.compare_gallery_with_wanted_filters(incoming_gallery3, incoming_gallery3.link, wanted_galleries, gallery_wanted_lists)
        parser.compare_gallery_with_wanted_filters(incoming_gallery4, incoming_gallery4.link, wanted_galleries, gallery_wanted_lists)

        self.assertEqual(len(gallery_wanted_lists[incoming_gallery2.gid]), 0)
        self.assertEqual(len(gallery_wanted_lists[incoming_gallery3.gid]), 1)
        self.assertEqual(len(gallery_wanted_lists[incoming_gallery4.gid]), 1)
