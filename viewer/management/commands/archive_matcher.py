import csv
import time

from django.core.management.base import BaseCommand
from django.conf import settings

from viewer.models import Archive, Gallery

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = "Batch process for Archives."

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_file", type=str, help=("Path to matcher CSV.")
        )

        parser.add_argument("-sh", "--skip-header", required=False, action="store_true", help=("Skip header."))

        parser.add_argument(
            "-ur",
            "--url-remove",
            required=False,
            help=("Text to remove from matching URLs."),
        )

        parser.add_argument("-ou", "--only-unmatched", required=False, action="store_true", help=("Only match unmatched Archives."))


    def handle(self, *args, **options):
        start = time.perf_counter()

        csv_file = options["csv_file"]
        skip_header = options["skip_header"]
        url_remove = options["url_remove"]
        only_unmatched = options["only_unmatched"]

        case_1_count = 0
        case_2_count = 0
        case_3_count = 0
        case_4_count = 0

        with open(csv_file, newline="") as csvfile:
            reader = csv.reader(csvfile)
            if skip_header:
                next(reader)
            for row in reader:
                if only_unmatched:
                    archive = Archive.objects.filter(zipped__contains=row[1], gallery__isnull=True).first()
                else:
                    archive = Archive.objects.filter(zipped__contains=row[1]).first()
                gallery_gid = row[0].replace(url_remove, "")
                gallery = Gallery.objects.filter(gid__contains=gallery_gid).first()
                if archive and gallery:
                    archive.gallery = gallery
                    archive.save()
                    print("Case 1: Archive: {} matched with Gallery: {}".format(row[1], row[0]))
                    case_1_count += 1
                elif archive:
                    print("Case 2: Archive: {} could not find Gallery: {}".format(row[1], row[0]))
                    case_2_count += 1
                elif gallery:
                    print("Case 3: Could not find Archive: {}, Gallery found: {}".format(row[1], row[0]))
                    case_3_count += 1
                else:
                    print("Case 4: Could not find Archive: {} nor Gallery: {}".format(row[1], row[0]))
                    case_4_count += 1

        end = time.perf_counter()

        self.stdout.write(self.style.SUCCESS("Case 1 total (Archive matched with Gallery)         : {}".format(case_1_count)))

        self.stdout.write(self.style.SUCCESS("Case 2 total (Archive not matched with Gallery)     : {}".format(case_2_count)))

        self.stdout.write(self.style.SUCCESS("Case 3 total (Archive not found, Gallery found)     : {}".format(case_3_count)))

        self.stdout.write(self.style.SUCCESS("Case 4 total (Archive not found, Gallery not found) : {}".format(case_4_count)))

        self.stdout.write(
            self.style.SUCCESS(
                "Time taken (seconds, minutes): {0:.2f}, {1:.2f}".format(end - start, (end - start) / 60)
            )
        )
