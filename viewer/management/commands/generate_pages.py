import time

from django.core.management.base import BaseCommand
from django.conf import settings
from django.db.models.aggregates import Avg, Max, Min, Sum, Count
from django.template.loader import render_to_string

from viewer.models import Gallery, Archive, Tag

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = 'Statically generate some heavy-processing pages.'

    def add_arguments(self, parser):
        parser.add_argument('-ps', '--public-stats',
                            required=False,
                            action='store',
                            help=(
                                'Statically render the public stats page. '))

    def handle(self, *args, **options):
        start = time.perf_counter()
        if options['public_stats']:
            self.generate_public_stats(options['public_stats'])

        end = time.perf_counter()

        self.stdout.write(
            self.style.SUCCESS(
                "Time taken (seconds, minutes): {0:.2f}, {1:.2f}".format(end - start, (end - start) / 60)
            )
        )

    def generate_public_stats(self, output_html: str) -> None:

        stats_dict = {
            "n_archives": Archive.objects.filter(public=True).count(),
            "n_galleries": Gallery.objects.filter(public=True).count(),
            "archive": Archive.objects.filter(public=True).filter(filesize__gt=0).aggregate(
                Avg('filesize'), Max('filesize'), Min('filesize'), Sum('filesize'), Avg('filecount'), Sum('filecount')),
            "gallery": Gallery.objects.filter(public=True).filter(filesize__gt=0).aggregate(
                Avg('filesize'), Max('filesize'), Min('filesize'), Sum('filesize'), Avg('filecount'), Sum('filecount')),
            "n_tags": Tag.objects.filter(gallery__public=True).distinct().count(),
            "top_10_tags": Tag.objects.filter(gallery__public=True).distinct().annotate(
                num_archive=Count('gallery')).order_by('-num_archive')[:10],
            "top_10_artist_tags": Tag.objects.filter(scope='artist', gallery__public=True).distinct().annotate(
                num_archive=Count('gallery')).order_by('-num_archive')[:10],
            "top_10_parody_tags": Tag.objects.filter(scope='parody', gallery__public=True).distinct().annotate(
                num_archive=Count('gallery')).order_by('-num_archive')[:10]
        }

        # Per category
        providers = Gallery.objects.filter(public=True).values_list('provider', flat=True).distinct()

        providers_dict = {}

        for provider in providers:
            providers_dict[provider] = {
                'n_galleries': Gallery.objects.filter(public=True, provider=provider).count(),
                'gallery': Gallery.objects.filter(public=True).filter(
                    filesize__gt=0, provider=provider
                ).aggregate(
                    Avg('filesize'), Max('filesize'), Min('filesize'), Sum('filesize'), Avg('filecount'),
                    Sum('filecount')
                )
            }

        # Per category
        categories = Gallery.objects.filter(public=True).values_list('category', flat=True).distinct()

        categories_dict = {}

        for category in categories:
            categories_dict[category] = {
                'n_galleries': Gallery.objects.filter(public=True, category=category).count(),
                'gallery': Gallery.objects.filter(public=True).filter(
                    filesize__gt=0, category=category
                ).aggregate(
                    Avg('filesize'), Max('filesize'), Min('filesize'), Sum('filesize'), Avg('filecount'),
                    Sum('filecount')
                )
            }

        # Per language tag
        languages = Tag.objects.filter(
            scope='language'
        ).exclude(
            scope='language', name='translated'
        ).annotate(num_gallery=Count('gallery')).order_by('-num_gallery').values_list('name', flat=True).distinct()

        languages_dict = {}

        languages_dict['untranslated'] = {
            'n_galleries': Gallery.objects.filter(public=True).exclude(tags__scope='language').distinct().count(),
            'gallery': Gallery.objects.filter(public=True).filter(
                filesize__gt=0, tags__scope='language'
            ).distinct().aggregate(
                Avg('filesize'), Max('filesize'), Min('filesize'), Sum('filesize'), Avg('filecount'), Sum('filecount')
            )
        }

        for language in languages:
            languages_dict[language] = {
                'n_galleries': Gallery.objects.filter(public=True).filter(tags__scope='language',
                                                                          tags__name=language).distinct().count(),
                'gallery': Gallery.objects.filter(public=True).filter(
                    filesize__gt=0, tags__scope='language', tags__name=language
                ).distinct().aggregate(
                    Avg('filesize'), Max('filesize'), Min('filesize'), Sum('filesize'), Avg('filecount'),
                    Sum('filecount')
                )
            }

        d = {
            'stats': stats_dict, 'gallery_categories': categories_dict, 'gallery_languages': languages_dict,
            'gallery_providers': providers_dict
        }

        content = render_to_string("viewer/static_public_stats.html", d)

        # TODO: User argument
        content = content.replace("/meta/static/", "https://static.chaika.moe/static/")
        content = content.replace("/meta/", "/")
        content = content.replace("output.4d110389f894.css", "output.c5266df505ca.css")

        with open(output_html, 'w', encoding='utf8') as static_file:
            static_file.write(content)
