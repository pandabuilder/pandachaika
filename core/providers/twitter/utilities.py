import re
import typing
from datetime import timedelta

from core.base.utilities import format_title_to_wanted_search
from viewer.models import TweetPost, WantedGallery, Artist
from . import constants

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


def match_tweet_with_wanted_galleries(tweet_obj: TweetPost, settings: 'Settings'):
    own_settings = settings.providers[constants.provider_name]

    publisher = 'wanimagazine'
    source = 'twitter'

    yield "Tweet id: {}, processing...".format(tweet_obj.tweet_id)

    match_tweet_type = re.search('【(.+)】(.*)', tweet_obj.text, re.DOTALL)
    if match_tweet_type:
        yield("Matched pattern (date_type: {}, title, artist: {}),".format(
            match_tweet_type.group(1).replace('\n', ''), match_tweet_type.group(2).replace('\n', ''))
        )
        release_type = None
        release_date = None
        date_type = re.search(r'.*?(\d+)/(\d+).*?', match_tweet_type.group(1), re.DOTALL)
        mention_date = tweet_obj.posted_date
        if date_type:
            release_type = 'release_date'
            release_date = mention_date.replace(
                month=int(date_type.group(1)), day=int(date_type.group(2)), hour=0, minute=0, second=0
            )
        new_book_type = re.search('新刊情報', match_tweet_type.group(1), re.DOTALL)
        if new_book_type:
            release_type = 'new_publication'
            release_date = tweet_obj.posted_date
        out_today_type = re.search('本日発売', match_tweet_type.group(1), re.DOTALL)
        if out_today_type:
            release_type = 'out_today'
            release_date = tweet_obj.posted_date
        out_tomorrow_type = re.search('明日発売', match_tweet_type.group(1), re.DOTALL)
        if out_tomorrow_type:
            release_type = 'out_tomorrow'
            release_date = tweet_obj.posted_date + timedelta(days=1)

        match_title_artists = re.search('^『(.+?)』は＜(.+)＞', match_tweet_type.group(2), re.DOTALL)
        if match_title_artists and release_type:

            yield("Matched pattern (title: {}, artists: {}), release_type: {}.".format(
                match_title_artists.group(1).replace('\n', ''),
                match_title_artists.group(2).replace('\n', ''),
                release_type)
            )

            title = match_title_artists.group(1)
            title = title.replace("X-EROS#", "X-EROS #")
            artists = set(match_title_artists.group(2).replace('ほか', '').split('/'))
            if len(artists) > 1:
                book_type = 'magazine'
            else:
                book_type = ''
            wanted_gallery, created = WantedGallery.objects.get_or_create(
                title_jpn=title,
                search_title=format_title_to_wanted_search(title),
                publisher=publisher,
                defaults={
                    'title': title,
                    'book_type': book_type,
                    'add_as_hidden': True,
                    'category': 'Manga',
                    'reason': 'wanimagazine',
                    'public': own_settings.add_as_public,
                    'unwanted_title': own_settings.unwanted_title or settings.auto_wanted.unwanted_title
                }
            )
            if created:
                wanted_gallery.should_search = True
                wanted_gallery.keep_searching = True
                wanted_gallery.save()
                yield(
                    "Created wanted gallery (magazine): {}, search title: {}".format(
                        wanted_gallery.get_absolute_url(),
                        title
                    )
                )
            mention, mention_created = wanted_gallery.mentions.get_or_create(
                mention_date=mention_date,
                release_date=release_date,
                type=release_type,
                source=source,
            )

            if mention_created:
                yield (
                    "Created mention for wanted gallery: {}, mention date: {}".format(
                        wanted_gallery.get_absolute_url(),
                        mention_date
                    )
                )

            if mention_created and tweet_obj.media_url:
                mention.save_img(tweet_obj.media_url)
                # wanted_gallery.calculate_nearest_release_date()
                wanted_gallery.release_date = release_date
                wanted_gallery.save()

            for artist in artists:
                artist_name = artist
                twitter_handle = ''
                match_artist_handle = re.search('^(.+?)（@(.+?)）', artist, re.DOTALL)
                if match_artist_handle:
                    artist_name = match_artist_handle.group(1)
                    twitter_handle = match_artist_handle.group(2)
                artist_obj = Artist.objects.filter(name_jpn=artist_name).first()
                if not artist_obj:
                    artist_obj = Artist.objects.create(
                        name=artist_name, name_jpn=artist_name, twitter_handle=twitter_handle
                    )
                wanted_gallery.artists.add(artist_obj)

        match_artist_title = re.search('^(.+?)『(.+?)』.*', match_tweet_type.group(2), re.DOTALL)
        if match_artist_title and release_type:

            yield("Matched pattern (artist: {}, title: {}), release type: {}.".format(
                match_artist_title.group(1).replace('\n', ''),
                match_artist_title.group(2).replace('\n', ''),
                release_type)
            )

            artist = match_artist_title.group(1)
            artist_name = artist
            twitter_handle = ''
            match_artist_handle = re.search('^(.+?)（@(.+?)）', artist, re.DOTALL)
            if match_artist_handle:
                artist_name = match_artist_handle.group(1)
                twitter_handle = match_artist_handle.group(2)
            title = match_artist_title.group(2)
            title = title.replace("X-EROS#", "X-EROS #")
            cover_artist = None
            book_type = None
            if '最新刊' in artist:
                artist_name = artist_name.replace('最新刊', '')
                book_type = 'new_publication'
                cover_artist = Artist.objects.filter(name_jpn=artist_name).first()
                if not cover_artist:
                    cover_artist = Artist.objects.create(
                        name=artist_name, name_jpn=artist_name, twitter_handle=twitter_handle
                    )
            elif '初単行本' in artist and ('『' not in artist and '』' not in artist):
                artist_name = artist_name.replace('初単行本', '')
                book_type = 'first_book'
                cover_artist = Artist.objects.filter(name_jpn=artist_name).first()
                if not cover_artist:
                    cover_artist = Artist.objects.create(
                        name=artist_name, name_jpn=artist_name, twitter_handle=twitter_handle
                    )
            elif '表紙が目印の' in artist:
                artist_name = artist_name.replace('表紙が目印の', '')
                book_type = "magazine"
                cover_artist = Artist.objects.filter(name_jpn=artist_name).first()
                if not cover_artist:
                    cover_artist = Artist.objects.create(
                        name=artist_name, name_jpn=artist_name, twitter_handle=twitter_handle
                    )
            if book_type:
                wanted_gallery, created = WantedGallery.objects.update_or_create(
                    title_jpn=title,
                    search_title=format_title_to_wanted_search(title),
                    publisher=publisher,
                    defaults={'cover_artist': cover_artist,
                              'title': title,
                              'book_type': book_type,
                              'add_as_hidden': True,
                              'category': 'Manga',
                              'reason': 'wanimagazine',
                              'public': own_settings.add_as_public}
                )
                if created:
                    wanted_gallery.should_search = True
                    wanted_gallery.keep_searching = True
                    wanted_gallery.save()
                    yield(
                        "Created wanted gallery (anthology): {}, search title: {}".format(
                            wanted_gallery.get_absolute_url(),
                            title
                        )
                    )
                mention, mention_created = wanted_gallery.mentions.get_or_create(
                    mention_date=mention_date,
                    release_date=release_date,
                    type=release_type,
                    source=source,
                )

                if mention_created:
                    yield (
                        "Created mention for wanted gallery: {}, mention date: {}".format(
                            wanted_gallery.get_absolute_url(),
                            mention_date
                        )
                    )

                if mention_created and tweet_obj.media_url:
                    mention.save_img(tweet_obj.media_url)
                    # wanted_gallery.calculate_nearest_release_date()
                    wanted_gallery.release_date = release_date
                    wanted_gallery.save()

                artist_obj = Artist.objects.filter(name_jpn=artist).first()
                if not artist_obj:
                    artist_obj = Artist.objects.create(
                        name=artist_name, name_jpn=artist_name, twitter_handle=twitter_handle
                    )
                wanted_gallery.artists.add(artist_obj)
