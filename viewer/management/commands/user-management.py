import time
from typing import Optional

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils.crypto import get_random_string

from viewer.models import UserLongLivedToken

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = 'Provider register management.'

    def add_arguments(self, parser):
        parser.add_argument('-un', '--user-name',
                            required=False,
                            action='store',
                            type=str,
                            help='User name to process.'
                            )
        parser.add_argument('-ui', '--user-id',
                            required=False,
                            action='store',
                            type=int,
                            help='User ID to process.'
                            )
        parser.add_argument('-ct', '--create-token',
                            required=False,
                            action='store',
                            type=str,
                            help='Create a token with the given name.'
                            )
        parser.add_argument('-dt', '--delete-token',
                            required=False,
                            action='store',
                            type=str,
                            help='Delete a token, by its name.'
                            )

    def handle(self, *args, **options):
        start = time.perf_counter()
        user = None
        if options['user_name']:
            try:
                user = User.objects.get(username=options['user_name'])
            except User.DoesNotExist:
                pass
        elif options['user_id']:
            user = User.objects.get(pk=options['user_id'])

        if not user:
            self.stdout.write(
                self.style.ERROR(
                    "Invalid user."
                )
            )
            return

        if options['create_token']:
            token_exists = user.long_lived_tokens.filter(name=options['create_token'])
            if len(token_exists) > 0:
                self.stdout.write(
                    self.style.ERROR(
                        "For user: {}, token with name: {} already exists.".format(user.username, options['create_token'])
                    )
                )
                return
            else:

                secret_key = get_random_string(UserLongLivedToken.TOKEN_LENGTH, UserLongLivedToken.VALID_CHARS)

                salted_key = UserLongLivedToken.create_salted_key_from_key(secret_key)

                token = UserLongLivedToken.objects.create(
                    key=salted_key,
                    user=user,
                    name=options['create_token'],
                )

                self.stdout.write(
                    self.style.SUCCESS(
                        "Successfully created token named: {} for user: {}. Do not lose this key, it will be only shown here: {}".format(token.name, user.username, secret_key)
                    )
                )

        elif options['delete_token']:
            token_exists = user.long_lived_tokens.filter(name=options['create_token'])
            if len(token_exists) < 1:
                self.stdout.write(
                    self.style.ERROR(
                        "For user: {}, token with name: {} doesn't exist.".format(user.name, options['create_token'])
                    )
                )
                return
            else:
                token_exists.delete()

        end = time.perf_counter()

        self.stdout.write(
            self.style.SUCCESS(
                "Time taken (seconds, minutes): {0:.2f}, {1:.2f}".format(end - start, (end - start) / 60)
            )
        )
