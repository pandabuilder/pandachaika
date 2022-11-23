from collections import defaultdict
from datetime import datetime, timezone

from django.test import TestCase

from core.base.setup import Settings
from core.base.types import GalleryData
from core.base.comparison import get_list_closer_gallery_titles_from_list
from core.providers.panda.parsers import Parser as PandaParser
from viewer.models import Gallery, WantedGallery, Tag, FoundGallery, Provider


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
        artist2_tag = Tag.objects.create(scope="artist", name="mitsuya")
        parody_tag = Tag.objects.create(scope="parody", name="original")

        provider_instance = Provider.objects.filter(slug='fakku').first()

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

        # Set 4
        self.test_wanted_gallery4 = WantedGallery.objects.create(
            title='test wanted gallery 4',
            book_type='Manga',
            publisher='wanimagazine',
            reason='wanimagazine',
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            wanted_tags_exclusive_scope=True,
            exclusive_scope_name='artist'
        )
        self.test_wanted_gallery4.wanted_tags.set([english_tag, artist2_tag])

        self.test_wanted_gallery4b = WantedGallery.objects.create(
            title='test wanted gallery 4b',
            book_type='Manga',
            publisher='wanimagazine',
            reason='wanimagazine',
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            wanted_tags_exclusive_scope=True,
            exclusive_scope_name='artist',
            wanted_tags_accept_if_none_scope='parody'
        )
        self.test_wanted_gallery4b.wanted_tags.set([artist2_tag, parody_tag])

        self.test_wanted_gallery4c = WantedGallery.objects.create(
            title='test wanted gallery 4c',
            book_type='Manga',
            publisher='wanimagazine',
            reason='wanimagazine',
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            wanted_tags_accept_if_none_scope='parody'
        )
        self.test_wanted_gallery4c.wanted_tags.set([artist2_tag, parody_tag])

        self.test_gallery4 = Gallery.objects.create(
            title='New Release 4', gid='34665', provider='panda', category='Manga',
            token='4324239'
        )
        self.test_gallery4.tags.set([english_tag, artist_tag, artist2_tag])

        self.test_gallery5 = Gallery.objects.create(
            title='New Release 5', gid='346659', provider='panda', category='Manga',
            token='4324288'
        )
        self.test_gallery5.tags.set([english_tag, artist2_tag, parody_tag])

        self.test_wanted_gallery5 = WantedGallery.objects.create(
            title='test wanted gallery 5',
            search_title='kairakuten',
            publisher='wanimagazine',
            reason='wanimagazine',
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            unwanted_title='[Chinese]'
        )

        if provider_instance:
            self.test_wanted_gallery5.unwanted_providers.add(provider_instance)

        self.test_gallery6 = Gallery.objects.create(
            title='COMIC Kairakuten 2022-06 [Digital]',
            title_jpn='COMIC 快楽天 2022年6月号 [DL版]',
            gid='2207323', token='95307725e5', provider='panda', category='Manga',
            posted=datetime.fromtimestamp(1651311800, timezone.utc),
            filecount=376,
            filesize=1043530781
        )

        # Regex
        self.test_wanted_gallery_regex = WantedGallery.objects.create(
            title='test wanted gallery regex',
            book_type='user',
            reason='kairakuten_raw',
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            search_title=r'COMIC Kairakuten \d{4}-\d{2}',
            regexp_search_title=True,
            regexp_search_title_icase=True,
            category='Manga',
            wanted_page_count_lower=100,
            wanted_page_count_upper=0,
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

        self.assertEqual(len(gallery_wanted_lists[incoming_gallery.gid]), 12)
        self.assertEqual(WantedGallery.objects.filter(found=True).count(), 13)

        incoming_gallery1 = self.test_gallery1.as_gallery_data()
        incoming_gallery2 = self.test_gallery2.as_gallery_data()
        incoming_gallery3 = self.test_gallery3.as_gallery_data()
        incoming_gallery4 = self.test_gallery4.as_gallery_data()
        incoming_gallery5 = self.test_gallery5.as_gallery_data()
        incoming_gallery6 = self.test_gallery6.as_gallery_data()

        gallery_wanted_lists: dict[str, list['WantedGallery']] = defaultdict(list)

        # 2 are not already found, remaining it is.
        parser.compare_gallery_with_wanted_filters(incoming_gallery1, incoming_gallery1.link, wanted_galleries, gallery_wanted_lists)
        parser.compare_gallery_with_wanted_filters(incoming_gallery2, incoming_gallery2.link, wanted_galleries, gallery_wanted_lists)
        parser.compare_gallery_with_wanted_filters(incoming_gallery3, incoming_gallery3.link, wanted_galleries, gallery_wanted_lists)
        parser.compare_gallery_with_wanted_filters(incoming_gallery4, incoming_gallery4.link, wanted_galleries, gallery_wanted_lists)
        parser.compare_gallery_with_wanted_filters(incoming_gallery5, incoming_gallery5.link, wanted_galleries, gallery_wanted_lists)
        parser.compare_gallery_with_wanted_filters(incoming_gallery6, incoming_gallery6.link, wanted_galleries, gallery_wanted_lists)

        self.assertEqual(len(gallery_wanted_lists[incoming_gallery1.gid]), 1)
        self.assertEqual(len(gallery_wanted_lists[incoming_gallery2.gid]), 1)
        self.assertEqual(len(gallery_wanted_lists[incoming_gallery3.gid]), 0)
        # Should be wg1 and wg4_c, but not wg4 and wg4_b, even if parody is set, should be rejected at multiple scopes,
        # don't accept repeated artist tag Gallery
        self.assertEqual(len(gallery_wanted_lists[incoming_gallery4.gid]), 2)
        # Should be wg4 , wg4_b and wg4_c, because it has only 1 artist tag, and even with parody missing,
        # it has acccept of none
        self.assertEqual(len(gallery_wanted_lists[incoming_gallery5.gid]), 3)
        # 1 + regex
        self.assertEqual(len(gallery_wanted_lists[incoming_gallery6.gid]), 2)

        rematch_result_1 = self.test_gallery1.match_against_wanted_galleries(wanted_galleries, skip_already_found=False)
        rematch_result_4 = self.test_gallery4.match_against_wanted_galleries(wanted_galleries, skip_already_found=False)
        rematch_result_5 = self.test_gallery5.match_against_wanted_galleries(wanted_galleries, skip_already_found=False)
        rematch_result_6 = self.test_gallery6.match_against_wanted_galleries(wanted_galleries, skip_already_found=False)

        # Make sure compare_gallery_with_wanted_filters gives the same result as match_against_wanted_galleries
        self.assertEqual(rematch_result_1, gallery_wanted_lists[incoming_gallery1.gid])
        self.assertEqual(rematch_result_4, gallery_wanted_lists[incoming_gallery4.gid])
        self.assertEqual(rematch_result_5, gallery_wanted_lists[incoming_gallery5.gid])
        self.assertEqual(rematch_result_6, gallery_wanted_lists[incoming_gallery6.gid])

        # Replicate the code inside core/base/handlers.py
        FoundGallery.objects.get_or_create(
            wanted_gallery=self.test_wanted_gallery5,
            gallery=self.test_gallery6
        )

        self.test_wanted_gallery5.found = True
        self.test_wanted_gallery5.save()

        wanted_found = WantedGallery.objects.filter(foundgallery__gallery=self.test_gallery6)

        rematch_wanted = self.test_gallery6.match_against_wanted_galleries(
            wanted_filters=wanted_found,
            skip_already_found=False
        )

        wanted_invalidated: list['WantedGallery'] = []

        for single_wanted_found in wanted_found:
            if single_wanted_found not in rematch_wanted:
                wanted_invalidated.append(single_wanted_found)

        self.assertEqual(len(wanted_invalidated), 0)
