import time

from django.core.management.base import BaseCommand
from django.conf import settings

from viewer.models import Archive

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = 'Batch process for Archives.'

    def add_arguments(self, parser):
        parser.add_argument('-id', '--id',
                            required=False,
                            action="extend", nargs="+", type=int,
                            help=(
                                'Process specific Archive ID. '))

        parser.add_argument('-a', '--all',
                            required=False,
                            action='store_true',
                            help=(
                                'Process all Archives. '))

        parser.add_argument('-data', '--data',
                            required=False,
                            action='store_true',
                            help=(
                                'Run the SHA1 and Data process for Archive Images. '))

    def handle(self, *args, **options):
        start = time.perf_counter()

        run_data = options['data']

        archives = None

        if options['id']:
            archives = Archive.objects.exclude(crc32='').filter(pk__in=options['id'])

        if options['all']:
            archives = Archive.objects.exclude(crc32='').all()

        if run_data and archives:
            self.stdout.write(
                "Create hashes and data for {} Archives".format(
                    archives.count(),
                )
            )
            for archive in archives:
                self.stdout.write(
                    "Working on Archive ID: {}".format(
                        archive.pk,
                    )
                )
                archive.calculate_sha1_and_data_for_images()

        end = time.perf_counter()

        self.stdout.write(
            self.style.SUCCESS(
                "Time taken (seconds, minutes): {0:.2f}, {1:.2f}".format(end - start, (end - start) / 60)
            )
        )
