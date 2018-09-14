import dateutil.parser
from django.test import TestCase

from core.base.setup import Settings
from core.base.types import GalleryData
from core.providers.fakku.parsers import Parser as FakkuParser
from core.providers.nhentai.parsers import Parser as NhentaiParser


class TestPageParsers(TestCase):
    maxDiff = None

    def test_nhentai_parser(self):
        """Test Nhentai gallery page parser"""
        settings = Settings(load_from_disk=True)

        gallery_link = 'https://nhentai.net/g/198482/'
        parser = NhentaiParser(settings)
        data = parser.fetch_gallery_data(gallery_link)

        expected_data = GalleryData(
            'nh-198482',
            title='(C90) [MeltdoWN COmet (Yukiu Con)] C90 Omakebon! (Pokémon GO) [English] [ATF]',
            title_jpn='(C90) [MeltdoWN COmet (雪雨こん)] C90 おまけ本! (ポケモンGO) [英訳]',
            filecount=9,
            link='https://nhentai.net/g/198482/',
            provider='nhentai',
            posted=dateutil.parser.parse('2017-06-19T10:33:19.022360+00:00'),
            category='Doujinshi',
            tags=[
                'parody:pokemon',
                'lolicon',
                'sole_female',
                'sole_male',
                'blowjob',
                'artist:yukiu_con',
                'group:meltdown_comet',
                'language:translated',
                'language:english',
            ]
        )

        self.assertEqual(data, expected_data)

    def test_fakku_parser(self):
        """Test FAKKU gallery page parser"""
        settings = Settings(load_from_disk=True)

        gallery_link = 'https://www.fakku.net/hentai/im-a-piece-of-junk-sexaroid-english'
        parser = FakkuParser(settings)
        data = parser.fetch_gallery_data(gallery_link)

        expected_data = GalleryData(
            'hentai/im-a-piece-of-junk-sexaroid-english',
            link=gallery_link,
            provider='fakku',
            title='I\'m a Piece of Junk Sexaroid',
            thumbnail_url='https://t.fakku.net/images/manga/i/im-a-piece-of-junk-sexaroid-english/thumbs/002.thumb.jpg',
            filecount=16,
            category='Manga',
            tags=[
                'artist:wakame-san',
                'magazine:comic_kairakuten_beast_2017-05',
                'publisher:fakku',
                'language:english',
                'tsundere',
                'femdom',
                'vanilla',
                'blowjob',
                'oppai',
                'hentai',
                'creampie',
                'uncensored',
                'x-ray',
                'subscription',
            ],
            comment='Plump slacker sex robot ❤',
        )

        self.assertEqual(data, expected_data)

        gallery_link = 'https://www.fakku.net/hentai/tsf-story-append-20-english_1497401155'
        parser = FakkuParser(settings)
        data = parser.fetch_gallery_data(gallery_link)

        expected_data = GalleryData(
            'hentai/tsf-story-append-20-english_1497401155',
            link=gallery_link,
            provider='fakku',
            title='TSF Story Append 2.0',
            filecount=82,
            category='Doujinshi',
            tags=[
                'artist:oda_non',
                'artist:yasui_riosuke',
                'artist:meme50',
                'artist:kojima_saya',
                'artist:butcha-u',
                'artist:mizuryu_kei',
                'artist:kurenai_yuuji',
                'artist:soine',
                'artist:asanagi',
                'artist:yumeno_tanuki',
                'artist:hiroyuki_sanadura',
                'artist:shindo_l',
                'artist:naokame',
                'artist:kin_no_hiyoko',
                'artist:masaru_yajiro',
                'group:da_hootch',
                'publisher:enshodo',
                'language:english',
                'anal',
                'blowjob',
                'oppai',
                'glasses',
                'stockings',
                'group',
                'nurse',
                'hentai',
                'ahegao',
                'creampie',
                'uncensored',
                'genderbend',
                'doujin',
            ],
            thumbnail_url='https://t.fakku.net/images/manga/t/tsf-story-append-20-english_1497401155_1502575464/thumbs/001.thumb.jpg',
        )

        self.assertEqual(data, expected_data)
