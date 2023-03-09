import logging

import typing
from datetime import datetime

from typing import Any

from django.db.models import Max, QuerySet
from twitter.api import Twitter
from twitter.oauth import OAuth

from viewer.models import TweetPost
from . import constants
from .utilities import HANDLES_MODULES

if typing.TYPE_CHECKING:
    from core.base.setup import Settings

logger = logging.getLogger(__name__)


CREDENTIALS = ('token', 'token_secret', 'consumer_key', 'consumer_secret')


def process_twitter_handle(handle_name, process_handle_tweets, t, tweet_posts):
    if tweet_posts:
        max_id = tweet_posts.aggregate(Max('tweet_id'))['tweet_id__max']
        while True:
            logger.info("Fetching since tweet id: {}".format(max_id))
            tweets = t.statuses.user_timeline(screen_name=handle_name, include_rts=False,
                                              exclude_replies=True, trim_user=True, count=200, since_id=max_id)
            if not tweets:
                logger.info("No more tweets to fetch, ending")
                break
            new_max_id = max(tweets, key=lambda x: x['id'])['id']
            for message in process_handle_tweets(tweets, handle_name):
                logger.info(message)
            if new_max_id == max_id:
                logger.info("No more new tweets fetched, stopping at: {}".format(max_id))
                break
            else:
                max_id = new_max_id
    else:
        min_id = None
        while True:
            if min_id:
                logger.info("Fetching backwards with max id: {}".format(min_id))
                tweets = t.statuses.user_timeline(screen_name=handle_name, include_rts=False,
                                                  exclude_replies=True, trim_user=True, count=200, max_id=min_id)
            else:
                logger.info("Starting from newer tweet.")
                tweets = t.statuses.user_timeline(screen_name=handle_name, include_rts=False,
                                                  exclude_replies=True, trim_user=True, count=200)
            if not tweets:
                logger.info("No more tweets to fetch, ending")
                break
            new_min_id = min(tweets, key=lambda x: x['id'])['id']
            for message in process_handle_tweets(tweets, handle_name):
                logger.info(message)
            if new_min_id == min_id:
                logger.info("No more new tweets fetched, stopping at: {}".format(min_id))
                break
            else:
                min_id = new_min_id


def wanted_generator(settings: 'Settings', attrs: QuerySet):
    own_settings = settings.providers[constants.provider_name]

    def process_handle_tweets(current_tweets: list[dict[str, Any]], handle_name):

        yield 'Parsing of {} tweets starting...'.format(len(current_tweets))

        for tweet in current_tweets:

            cover_url = None
            if 'media' in tweet['entities']:
                for media in tweet['entities']['media']:
                    cover_url = media['media_url']

            tweet_obj, tweet_created = TweetPost.objects.get_or_create(
                tweet_id=tweet['id'],
                defaults={'text': tweet['text'],
                          'user': handle_name,
                          'posted_date': datetime.strptime(tweet['created_at'], "%a %b %d %H:%M:%S %z %Y"),
                          'media_url': cover_url}
            )

            if not tweet_created:
                continue

            if handle_name in HANDLES_MODULES:
                yield from HANDLES_MODULES[handle_name].match_tweet_with_wanted_galleries(tweet_obj, settings, own_settings)

    if not all([getattr(own_settings, x) for x in CREDENTIALS]):
        logger.error('Cannot work with Twitter unless all credentials are set.')
        return

    t = Twitter(
        auth=OAuth(
            own_settings.token,
            own_settings.token_secret,
            own_settings.consumer_key,
            own_settings.consumer_secret,
        )
    )

    for handle_name in own_settings.enabled_handles:
        if handle_name not in HANDLES_MODULES:
            logger.error("Configured Twitter handle {} is not supported".format(handle_name))
            continue
        tweet_posts = TweetPost.objects.filter(user=handle_name)
        process_twitter_handle(handle_name, process_handle_tweets, t, tweet_posts)

