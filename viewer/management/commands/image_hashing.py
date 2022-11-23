import time

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.conf import settings

from viewer.models import Gallery, Archive, Image, ItemProperties

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = 'Batch process thumbnail hashes.'

    def add_arguments(self, parser):
        parser.add_argument('-a', '--archive',
                            required=False,
                            action='store_true',
                            help=(
                                'Create for Archives. '))

        parser.add_argument('-g', '--gallery',
                            required=False,
                            action='store_true',
                            help=(
                                'Create for Galleries. '))

        parser.add_argument('-i', '--image',
                            required=False,
                            action='store_true',
                            help=(
                                'Create for Images. '))

        parser.add_argument('-f', '--force',
                            required=False,
                            action='store_true',
                            help=(
                                'Force all. If not, run for missing only. '))

        parser.add_argument('-al', '--algorithm',
                            required=True,
                            action='store',
                            help=(
                                'Algorithm to hash. '))

    def handle(self, *args, **options):
        start = time.perf_counter()

        algorithm = options['algorithm']

        if options['archive']:
            if not options['force']:
                archive_type = ContentType.objects.get_for_model(Archive)
                current_archives = ItemProperties.objects.filter(
                    content_type=archive_type, tag='hash-compare',
                    name=algorithm
                ).values_list('object_id', flat=True)
                archives = Archive.objects.exclude(pk__in=current_archives).exclude(thumbnail='')
            else:
                archives = Archive.objects.exclude(thumbnail='')

            self.stdout.write(
                "Create hashes for {} Archives using hash: {}".format(
                    archives.count(),
                    algorithm
                )
            )

            for archive in archives:
                archive.create_or_update_thumbnail_hash(algorithm)

        if options['gallery']:
            if not options['force']:
                gallery_type = ContentType.objects.get_for_model(Gallery)
                current_galleries = ItemProperties.objects.filter(
                    content_type=gallery_type, tag='hash-compare',
                    name=algorithm
                ).values_list('object_id', flat=True)
                galleries = Gallery.objects.exclude(pk__in=current_galleries).exclude(thumbnail='')
            else:
                galleries = Gallery.objects.exclude(thumbnail='')

            self.stdout.write(
                "Create hashes for {} Galleries using hash: {}".format(
                    galleries.count(),
                    algorithm
                )
            )

            for gallery in galleries:
                gallery.create_or_update_thumbnail_hash(algorithm)

        if options['image']:
            if not options['force']:
                image_type = ContentType.objects.get_for_model(Image)
                current_images = ItemProperties.objects.filter(content_type=image_type, tag='hash-compare', name=algorithm).values_list('object_id', flat=True)
                images = Image.objects.exclude(pk__in=current_images)
            else:
                images = Image.objects.all()

            self.stdout.write(
                "Create hashes for {} Images using hash: {}".format(
                    images.count(),
                    algorithm
                )
            )

            for image in images:
                image.create_or_update_thumbnail_hash(algorithm)

        end = time.perf_counter()

        self.stdout.write(
            self.style.SUCCESS(
                "Time taken (seconds, minutes): {0:.2f}, {1:.2f}".format(end - start, (end - start) / 60)
            )
        )
