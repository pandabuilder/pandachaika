import time

from django.core.management.base import BaseCommand
from django.conf import settings

from core.providers.twitter.utilities import HANDLES_MODULES
from core.providers.twitter import constants
from viewer.models import TweetPost

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = "Management tools for Twitter posts."

    def add_arguments(self, parser):
        parser.add_argument(
            "-fi", "--from_id", required=False, action="store", type=int, help="Starting tweet id to process."
        )
        parser.add_argument(
            "-ti", "--to_id", required=False, action="store", default="", help="Ending tweet id to process."
        )
        parser.add_argument(
            "-id", "--id", required=False, action="store", nargs="+", help="One or more tweet id to process."
        )
        parser.add_argument(
            "-w",
            "--wanted",
            required=False,
            action="store_true",
            default=False,
            help="Reprocess tweets against Wanted Galleries.",
        )

    def handle(self, *args, **options):
        start = time.perf_counter()

        tweets = TweetPost.objects.all()

        if options["from_id"]:
            tweets = tweets.filter(tweet_id__gte=options["from_id"])
        if options["to_id"]:
            tweets = tweets.filter(tweet_id__lte=options["to_id"])
        if options["id"]:
            tweets = tweets.filter(tweet_id__in=options["id"])

        if options["wanted"]:
            own_settings = crawler_settings.providers[constants.provider_name]

            for tweet_obj in tweets:
                if tweet_obj.user in HANDLES_MODULES:
                    for message in HANDLES_MODULES[tweet_obj.user].match_tweet_with_wanted_galleries(
                        tweet_obj, crawler_settings, own_settings
                    ):
                        self.stdout.write(message)

        end = time.perf_counter()

        self.stdout.write(
            self.style.SUCCESS(
                "Time taken (seconds, minutes): {0:.2f}, {1:.2f}".format(end - start, (end - start) / 60)
            )
        )
