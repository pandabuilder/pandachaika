import re
import typing
from datetime import timedelta

import bs4
from bs4 import BeautifulSoup

from core.base.utilities import format_title_to_wanted_search, request_with_retries
from viewer.models import TweetPost, WantedGallery, Artist
from .. import constants

if typing.TYPE_CHECKING:
    from core.base.setup import Settings
    from ..settings import OwnSettings

HANDLE = "wanimagazine"


def get_image_link_from_tweet_text(tweet_text: str, settings: "Settings") -> typing.Optional[str]:
    tweet_links = re.findall(r"https://t.co/\w+", tweet_text)
    for tweet_link in tweet_links:
        request_dict = {"timeout": settings.timeout_timer, "allow_redirects": False}
        r = request_with_retries(
            tweet_link,
            request_dict,
            post=False,
        )
        if not r:
            return None
        if "Location" in r.headers:
            if r.headers["Location"].startswith("https://www.wani.com/product/"):
                request_dict_image = {
                    "headers": settings.requests_headers,
                    "timeout": settings.timeout_timer,
                }
                product_page = request_with_retries(
                    r.headers["Location"],
                    request_dict_image,
                    post=False,
                )
                if not product_page:
                    return None
                product_page.encoding = "utf-8"
                soup = BeautifulSoup(product_page.text, "html.parser")
                product_head = soup.find("head")
                if isinstance(product_head, bs4.element.Tag):
                    img_container = product_head.find("meta", property="og:image")
                    if isinstance(img_container, bs4.element.Tag) and isinstance(img_container["content"], str):
                        return img_container["content"]

    return None


def match_tweet_with_wanted_galleries(tweet_obj: TweetPost, settings: "Settings", own_settings: "OwnSettings"):

    yield "Tweet id: {}, processing...".format(tweet_obj.tweet_id)
    wanted_reason = HANDLE

    if not tweet_obj.text:
        yield "Created tweet id: {} did not contain text to analyze".format(tweet_obj.tweet_id)
        return

    match_tweet_type = re.search("【(.+)】(.*)", tweet_obj.text, re.DOTALL)
    if match_tweet_type:
        yield (
            "Matched pattern (date_type: {}, title, artist: {}),".format(
                match_tweet_type.group(1).replace("\n", ""), match_tweet_type.group(2).replace("\n", "")
            )
        )
        release_type = None
        release_date = None
        date_type = re.search(r".*?(\d+)/(\d+).*?", match_tweet_type.group(1), re.DOTALL)
        mention_date = tweet_obj.posted_date
        if date_type:
            release_type = "release_date"
            if mention_date:
                release_date = mention_date.replace(
                    month=int(date_type.group(1)), day=int(date_type.group(2)), hour=0, minute=0, second=0
                )
            else:
                mention_date = mention_date
        new_book_type = re.search("新刊情報", match_tweet_type.group(1), re.DOTALL)
        if new_book_type:
            release_type = "new_publication"
            release_date = tweet_obj.posted_date
        out_today_type = re.search("本日発売", match_tweet_type.group(1), re.DOTALL)
        if out_today_type:
            release_type = "out_today"
            release_date = tweet_obj.posted_date
        out_tomorrow_type = re.search("明日発売", match_tweet_type.group(1), re.DOTALL)
        if out_tomorrow_type:
            release_type = "out_tomorrow"
            if tweet_obj.posted_date:
                release_date = tweet_obj.posted_date + timedelta(days=1)
            else:
                release_date = tweet_obj.posted_date

        match_title_artists = re.search("^『(.+?)』は＜(.+)＞", match_tweet_type.group(2), re.DOTALL)
        if match_title_artists and release_type:

            yield (
                "Matched pattern (title: {}, artists: {}), release_type: {}.".format(
                    match_title_artists.group(1).replace("\n", ""),
                    match_title_artists.group(2).replace("\n", ""),
                    release_type,
                )
            )

            title = match_title_artists.group(1)
            title = title.replace("X-EROS#", "X-EROS #")
            artists = set(match_title_artists.group(2).replace("ほか", "").split("/"))
            if len(artists) > 1:
                book_type = "magazine"
            else:
                book_type = ""
            wanted_gallery, created = WantedGallery.objects.get_or_create(
                title_jpn=title,
                search_title=format_title_to_wanted_search(title),
                publisher=HANDLE,
                defaults={
                    "title": title,
                    "book_type": book_type,
                    "category": "Manga",
                    "reason": wanted_reason,
                    "public": own_settings.add_as_public,
                    "unwanted_title": own_settings.unwanted_title or settings.auto_wanted.unwanted_title,
                    "regexp_unwanted_title": own_settings.regexp_unwanted_title or settings.auto_wanted.regexp_unwanted_title,
                    "regexp_unwanted_title_icase": own_settings.regexp_unwanted_title_icase or settings.auto_wanted.regexp_unwanted_title_icase,
                },
            )
            if created:
                wanted_gallery.should_search = True
                wanted_gallery.keep_searching = True
                wanted_gallery.save()
                yield (
                    "Created wanted gallery (magazine): {}, search title: {}".format(
                        wanted_gallery.get_absolute_url(), title
                    )
                )
            mention, mention_created = wanted_gallery.mentions.get_or_create(
                mention_date=mention_date,
                release_date=release_date,
                type=release_type,
                source=constants.provider_name,
            )

            if mention_created:
                yield (
                    "Created mention for wanted gallery: {}, mention date: {}".format(
                        wanted_gallery.get_absolute_url(), mention_date
                    )
                )
                wanted_gallery.release_date = release_date
                wanted_gallery.save()

            if not mention.thumbnail:
                if tweet_obj.media_url:
                    mention.save_img(tweet_obj.media_url)
                else:
                    img_link = get_image_link_from_tweet_text(tweet_obj.text, settings)
                    if img_link:
                        mention.save_img(img_link)

            for artist in artists:
                artist_name = artist
                twitter_handle = ""
                match_artist_handle = re.search("^(.+?)（@(.+?)）", artist, re.DOTALL)
                if match_artist_handle:
                    artist_name = match_artist_handle.group(1)
                    twitter_handle = match_artist_handle.group(2)
                artist_obj = Artist.objects.filter(name_jpn=artist_name).first()
                if not artist_obj:
                    artist_obj = Artist.objects.create(
                        name=artist_name, name_jpn=artist_name, twitter_handle=twitter_handle
                    )
                wanted_gallery.artists.add(artist_obj)

        match_artist_title = re.search("^(.+?)『(.+?)』.*", match_tweet_type.group(2), re.DOTALL)
        if match_artist_title and release_type:

            yield (
                "Matched pattern (artist: {}, title: {}), release type: {}.".format(
                    match_artist_title.group(1).replace("\n", ""),
                    match_artist_title.group(2).replace("\n", ""),
                    release_type,
                )
            )

            artist = match_artist_title.group(1)
            artist_name = artist
            twitter_handle = ""
            match_artist_handle = re.search("^(.+?)（@(.+?)）", artist, re.DOTALL)
            if match_artist_handle:
                artist_name = match_artist_handle.group(1)
                twitter_handle = match_artist_handle.group(2)
            title = match_artist_title.group(2)
            title = title.replace("X-EROS#", "X-EROS #")
            cover_artist = None
            book_type = ""
            if "最新刊" in artist:
                artist_name = artist_name.replace("最新刊", "")
                book_type = "new_publication"
                cover_artist = Artist.objects.filter(name_jpn=artist_name).first()
                if not cover_artist:
                    cover_artist = Artist.objects.create(
                        name=artist_name, name_jpn=artist_name, twitter_handle=twitter_handle
                    )
            elif "初単行本" in artist and ("『" not in artist and "』" not in artist):
                artist_name = artist_name.replace("初単行本", "")
                book_type = "first_book"
                cover_artist = Artist.objects.filter(name_jpn=artist_name).first()
                if not cover_artist:
                    cover_artist = Artist.objects.create(
                        name=artist_name, name_jpn=artist_name, twitter_handle=twitter_handle
                    )
            elif "表紙が目印の" in artist:
                artist_name = artist_name.replace("表紙が目印の", "")
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
                    publisher=HANDLE,
                    defaults={
                        "cover_artist": cover_artist,
                        "title": title,
                        "book_type": book_type,
                        "category": "Manga",
                        "reason": wanted_reason,
                        "public": own_settings.add_as_public,
                    },
                )
                if created:
                    wanted_gallery.should_search = True
                    wanted_gallery.keep_searching = True
                    wanted_gallery.save()
                    yield (
                        "Created wanted gallery (anthology): {}, search title: {}".format(
                            wanted_gallery.get_absolute_url(), title
                        )
                    )
                mention, mention_created = wanted_gallery.mentions.get_or_create(
                    mention_date=mention_date,
                    release_date=release_date,
                    type=release_type,
                    source=constants.provider_name,
                )

                if mention_created:
                    yield (
                        "Created mention for wanted gallery: {}, mention date: {}".format(
                            wanted_gallery.get_absolute_url(), mention_date
                        )
                    )
                    wanted_gallery.release_date = release_date
                    wanted_gallery.save()

                if not mention.thumbnail:
                    if tweet_obj.media_url:
                        mention.save_img(tweet_obj.media_url)
                    else:
                        img_link = get_image_link_from_tweet_text(tweet_obj.text, settings)
                        if img_link:
                            mention.save_img(img_link)

                artist_obj = Artist.objects.filter(name_jpn=artist_name).first()
                if not artist_obj:
                    artist_obj = Artist.objects.create(
                        name=artist_name, name_jpn=artist_name, twitter_handle=twitter_handle
                    )
                wanted_gallery.artists.add(artist_obj)
            else:
                yield "Book type could not be determined, skipping."
    else:
        yield "Created tweet id: {} did not match the pattern".format(tweet_obj.tweet_id)
