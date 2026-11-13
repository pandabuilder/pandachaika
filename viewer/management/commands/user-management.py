import time
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone as django_tz

from viewer.models import UserLongLivedToken

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = "Provider register management."

    def add_arguments(self, parser):
        parser.add_argument(
            "-un", "--user-name", required=False, action="store", type=str, help="User name to process."
        )
        parser.add_argument("-ui", "--user-id", required=False, action="store", type=int, help="User ID to process.")
        parser.add_argument(
            "-ct",
            "--create-token",
            required=False,
            action="store",
            type=str,
            help="Create a token with the given name.",
        )
        parser.add_argument(
            "-dt", "--delete-token", required=False, action="store", type=str, help="Delete a token, by its name."
        )
        parser.add_argument(
            "--token-expiry-days",
            required=False,
            action="store",
            type=int,
            default=3650,
            help="Days until the token created with --create-token expires. 0 creates a token that never expires.",
        )
        parser.add_argument(
            "--token-scopes",
            required=False,
            action="store",
            type=str,
            default=",".join(UserLongLivedToken.ALL_SCOPES),
            help="Comma separated scopes for the token created with --create-token (default: all). "
            "Available: {}.".format(", ".join(UserLongLivedToken.ALL_SCOPES)),
        )

    def handle(self, *args, **options):
        start = time.perf_counter()
        user = None
        if options["user_name"]:
            try:
                user = User.objects.get(username=options["user_name"])
            except User.DoesNotExist:
                pass
        elif options["user_id"]:
            user = User.objects.get(pk=options["user_id"])

        if not user:
            self.stdout.write(self.style.ERROR("Invalid user."))
            return

        if options["create_token"]:
            token_exists = user.long_lived_tokens.filter(name=options["create_token"])
            if len(token_exists) > 0:
                self.stdout.write(
                    self.style.ERROR(
                        "For user: {}, token with name: {} already exists.".format(
                            user.username, options["create_token"]
                        )
                    )
                )
                return
            else:
                scopes = [scope.strip() for scope in options["token_scopes"].split(",") if scope.strip()]
                invalid_scopes = [scope for scope in scopes if scope not in UserLongLivedToken.ALL_SCOPES]
                if invalid_scopes or not scopes:
                    self.stdout.write(
                        self.style.ERROR(
                            "Invalid scopes: {}. Available: {}.".format(
                                ", ".join(invalid_scopes) or "none given", ", ".join(UserLongLivedToken.ALL_SCOPES)
                            )
                        )
                    )
                    return
                if options["token_expiry_days"] < 0:
                    self.stdout.write(self.style.ERROR("--token-expiry-days cannot be negative."))
                    return

                expire_date = (
                    django_tz.now() + timedelta(days=options["token_expiry_days"])
                    if options["token_expiry_days"]
                    else None
                )
                token, secret_key = UserLongLivedToken.create_for_user(
                    user, options["create_token"], expire_date=expire_date, scopes=scopes
                )

                self.stdout.write(
                    self.style.SUCCESS(
                        "Successfully created token named: {} for user: {}. Do not lose this key, it will be only shown here: {}".format(
                            token.name, user.username, secret_key
                        )
                    )
                )

        elif options["delete_token"]:
            token_exists = user.long_lived_tokens.filter(name=options["delete_token"])
            if len(token_exists) < 1:
                self.stdout.write(
                    self.style.ERROR(
                        "For user: {}, token with name: {} doesn't exist.".format(user.username, options["delete_token"])
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
