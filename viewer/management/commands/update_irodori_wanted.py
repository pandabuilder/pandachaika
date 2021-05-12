import os
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.conf import settings

from viewer.models import WantedGallery, Tag, Provider

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = 'Update irodori raw and English galleries to watch for, from an artist list.'

    def add_arguments(self, parser):
        parser.add_argument('-al', '--artist_list',
                            required=False,
                            action='store',
                            nargs='+',
                            type=str,
                            help='Specify list of artists to add.')
        parser.add_argument('-af', '--artist_file',
                            required=False,
                            action='store',
                            help='File path containing arists\' names.')
        parser.add_argument('-wp', '--wanted_providers',
                            required=False,
                            action='store',
                            nargs='+',
                            type=str,
                            help='Specify list of wanted providers to search for.')
        parser.add_argument('-ss', '--should_search',
                            required=False,
                            action='store_true',
                            default=False,
                            help='Add WantedGallery with should_search on.')

    def handle(self, *args, **options):
        start = time.perf_counter()

        if options['wanted_providers']:
            providers = Provider.objects.filter(slug__in=options['wanted_providers'])
        else:
            providers = []

        artists = []

        if options['artist_list']:
            artists += options['artist_list']

        if options['artist_file'] and os.path.isfile(options['artist_file']):
            with open(options['artist_file'], 'r') as artist_file:
                artists_name = [line.rstrip() for line in artist_file.readlines()]
                artists += artists_name

        parody_tag, _ = Tag.objects.get_or_create(scope="parody", name="original")

        already_uploaded_tag, _ = Tag.objects.get_or_create(name="already_uploaded")
        forbidden_content_tag, _ = Tag.objects.get_or_create(name="forbidden_content")
        english_tag, _ = Tag.objects.get_or_create(scope="language", name="english")

        unwanted_tags = [
            already_uploaded_tag,
            forbidden_content_tag
        ]

        for artist in artists:
            self.stdout.write("Analizing arist: {}".format(artist))
            # Some artists are group in panda, others artist
            # We expect the tag to already exist, it is not created from here
            artist_tag = None
            if Tag.objects.filter(scope="group", name=artist).first():
                artist_tag = Tag.objects.filter(scope="group", name=artist).first()
            if Tag.objects.filter(scope="artist", name=artist).first():
                artist_tag = Tag.objects.filter(scope="artist", name=artist).first()

            if not artist_tag:
                self.stdout.write("Skipping arist: {}, since it doesn't have a tag.".format(artist))
                continue

            obj_raw, created = WantedGallery.objects.get_or_create(
                title='Irodori artist RAW: {}'.format(artist),
                book_type='user_irodori_raw',
                publisher='irodori',
                category="Doujinshi",
                reason='irodori',
                defaults={
                    'should_search': options['should_search'],
                    'keep_searching': True,
                    'notify_when_found': False,
                    'public': False,
                    'add_as_hidden': True,
                    'wait_for_time': timedelta(hours=2),
                    'release_date': None,
                    'wanted_tags_exclusive_scope': True,
                    'exclusive_scope_name': artist_tag.scope,
                    'wanted_tags_accept_if_none_scope': 'parody',
                },
            )

            if created:
                translated_tag, _ = Tag.objects.get_or_create(scope="language", name="translated")
                # wanted artist, parody, unwanted translated
                obj_raw.wanted_tags.add(artist_tag)
                obj_raw.unwanted_tags.add(translated_tag)
                obj_raw.unwanted_tags.add(english_tag)
                obj_raw.wanted_tags.add(parody_tag)
                for unwanted_tag in unwanted_tags:
                    obj_raw.unwanted_tags.add(unwanted_tag)
                if providers:
                    obj_raw.wanted_providers.set(providers)
                self.stdout.write("Added WantedGallery: {}".format(obj_raw.title))
            else:
                self.stdout.write("Skipping arist: {}, since it was already created.".format(artist))

            obj_english, created = WantedGallery.objects.get_or_create(
                title='Irodori artist English: {}'.format(artist),
                book_type='user_irodori_eng',
                publisher='irodori',
                category="Doujinshi",
                reason='irodori',
                defaults={
                    'should_search': options['should_search'],
                    'keep_searching': True,
                    'notify_when_found': False,
                    'public': False,
                    'add_as_hidden': True,
                    'wait_for_time': timedelta(hours=2),
                    'release_date': None,
                    'wanted_tags_exclusive_scope': True,
                    'exclusive_scope_name': artist_tag.scope,
                    'wanted_tags_accept_if_none_scope': 'parody',
                },
            )

            if created:
                # wanted artist, parody, english
                obj_english.wanted_tags.add(artist_tag)
                obj_english.wanted_tags.add(english_tag)
                obj_english.wanted_tags.add(parody_tag)

                for unwanted_tag in unwanted_tags:
                    obj_english.unwanted_tags.add(unwanted_tag)
                if providers:
                    obj_english.wanted_providers.set(providers)
                self.stdout.write("Added WantedGallery: {}".format(obj_english.title))
            else:
                self.stdout.write("Skipping arist: {}, since it was already created.".format(artist))

        end = time.perf_counter()

        self.stdout.write(
            self.style.SUCCESS(
                "Time taken (seconds, minutes): {0:.2f}, {1:.2f}".format(end - start, (end - start) / 60)
            )
        )
