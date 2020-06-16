import logging
import typing
from datetime import datetime
from typing import Any, Dict, List

from django.db.models import Max, QuerySet
from twitter import Twitter, OAuth

from viewer.models import TweetPost
from . import constants, utilities

if typing.TYPE_CHECKING:
    from core.base.setup import Settings

logger = logging.getLogger(__name__)


CREDENTIALS = ('token', 'token_secret', 'consumer_key', 'consumer_secret')


def wanted_generator(settings: 'Settings', attrs: QuerySet):
    own_settings = settings.providers[constants.provider_name]

    def process_wani_tweets(current_tweets: List[Dict[str, Any]]):
        publisher = 'wanimagazine'

        yield('Parsing of {} tweets starting...'.format(len(current_tweets)))

        for tweet in current_tweets:

            cover_url = None
            if 'media' in tweet['entities']:
                for media in tweet['entities']['media']:
                    cover_url = media['media_url']

            tweet_obj, tweet_created = TweetPost.objects.get_or_create(
                tweet_id=tweet['id'],
                defaults={'text': tweet['text'],
                          'user': publisher,
                          'posted_date': datetime.strptime(tweet['created_at'], "%a %b %d %H:%M:%S %z %Y"),
                          'media_url': cover_url}
            )

            if not tweet_created:
                continue

            yield from utilities.match_tweet_with_wanted_galleries(tweet_obj, settings, own_settings)

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
    tweet_posts = TweetPost.objects.all()
    if tweet_posts:
        max_id = tweet_posts.aggregate(Max('tweet_id'))['tweet_id__max']
        while True:
            logger.info("Fetching since tweet id: {}".format(max_id))
            tweets = t.statuses.user_timeline(screen_name='wanimagazine', include_rts=False,
                                              exclude_replies=True, trim_user=True, count=200, since_id=max_id)
            if not tweets:
                logger.info("No more tweets to fetch, ending")
                break
            new_max_id = max(tweets, key=lambda x: x['id'])['id']
            for message in process_wani_tweets(tweets):
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
                tweets = t.statuses.user_timeline(screen_name='wanimagazine', include_rts=False,
                                                  exclude_replies=True, trim_user=True, count=200, max_id=min_id)
            else:
                logger.info("Starting from newer tweet.")
                tweets = t.statuses.user_timeline(screen_name='wanimagazine', include_rts=False,
                                                  exclude_replies=True, trim_user=True, count=200)
            if not tweets:
                logger.info("No more tweets to fetch, ending")
                break
            new_min_id = min(tweets, key=lambda x: x['id'])['id']
            for message in process_wani_tweets(tweets):
                logger.info(message)
            if new_min_id == min_id:
                logger.info("No more new tweets fetched, stopping at: {}".format(min_id))
                break
            else:
                min_id = new_min_id
