import time
from types import ModuleType

from django.core.management.base import BaseCommand
from django.conf import settings
from viewer.models import Provider

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = 'Provider register management.'

    def add_arguments(self, parser):
        parser.add_argument('-sr', '--scan-register',
                            required=False,
                            action='store_true',
                            default=False,
                            help=(
                                'Scan and register all new providers to the database. '
                                'Already registered ones will be skipped.'))
        parser.add_argument('-r', '--register',
                            required=False,
                            action='store',
                            nargs='+',
                            type=str,
                            help=(
                                'Register all new providers that match the list to the database. '
                                'Already registered ones will be skipped.'))
        parser.add_argument('-d', '--delete',
                            required=False,
                            action='store',
                            nargs='+',
                            type=str,
                            help='Delete providers that match the list from the database.')
        parser.add_argument('-s', '--show',
                            required=False,
                            action='store_true',
                            default=False,
                            help='Display current registered providers in the database.')

    def handle(self, *args, **options):
        start = time.perf_counter()
        if options['scan_register']:
            for provider_name, constants in crawler_settings.provider_context.constants:
                self.register_provider_from_constants(constants, provider_name)
        if options['register']:
            for provider_to_register in options['register']:
                constants_list = crawler_settings.provider_context.get_constants(provider_to_register)
                for constants in constants_list:
                    provider_name = getattr(constants, "provider_name")
                    self.register_provider_from_constants(constants, provider_name)
        if options['delete']:
            providers = Provider.objects.all(name__in=options['delete'])
            self.stdout.write('Deleting {} providers.'.format(providers.count()))
            providers.delete()
        if options['show']:
            for provider in Provider.objects.all():
                self.stdout.write(
                    "Provider: {}, slug: {}, home page: {}, description: {}, information: {}".format(
                        provider.name,
                        provider.slug,
                        provider.home_page,
                        provider.description,
                        provider.information,
                    )
                )

        end = time.perf_counter()

        self.stdout.write(
            self.style.SUCCESS(
                "Time taken (seconds, minutes): {0:.2f}, {1:.2f}".format(end - start, (end - start) / 60)
            )
        )

    def register_provider_from_constants(self, constants: ModuleType, provider_name: str) -> None:
        provider_instance = Provider.objects.filter(slug=provider_name).first()
        if not provider_instance:
            self.stdout.write('Registering new provider: {}'.format(provider_name))
            Provider.objects.create(
                slug=getattr(constants, "provider_name") if hasattr(constants,
                                                                    "provider_name") else provider_name,
                name=getattr(constants, "friendly_name") if hasattr(constants,
                                                                    "friendly_name") else provider_name,
                home_page=getattr(constants, "home_page") if hasattr(constants, "home_page") else '',
                description=getattr(constants, "description") if hasattr(constants, "description") else '',
                information=getattr(constants, "information") if hasattr(constants, "information") else '',
            )
