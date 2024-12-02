import os
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.conf import settings

from viewer.models import WantedGallery, Tag, Provider, Category

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = 'Update any provider raw and English galleries to watch for, from an artist list.'

    def add_arguments(self, parser):
        parser.add_argument('provider',
                            type=str,
                            help='Provider name to add.')
        parser.add_argument('-al', '--artist_list',
                            required=False,
                            action='store',
                            nargs='+',
                            type=str,
                            help='Specify list of artists to add.')
        parser.add_argument('-af', '--artist_file',
                            required=False,
                            action='store',
                            help='File path containing artists\' names.')
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
        parser.add_argument('-ca', '--categories',
                            required=False,
                            nargs='*',
                            default=['Doujinshi'],
                            help='What categories to search for, Defaults to Doujinshi.')

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

        already_uploaded_tag, _ = Tag.objects.get_or_create(scope="other", name="already_uploaded")
        forbidden_content_tag, _ = Tag.objects.get_or_create(scope="other", name="forbidden_content")
        english_tag, _ = Tag.objects.get_or_create(scope="language", name="english")

        unwanted_tags = [
            already_uploaded_tag,
            forbidden_content_tag
        ]

        categories = []

        for category in options['categories']:
            category_obj, _ = Category.objects.get_or_create(name=category)
            categories.append(category_obj)

        for artist in artists:
            self.stdout.write("Analyzing artist/group: {}".format(artist))
            # Some artists are group in panda, others artist
            # We expect the tag to already exist, it is not created from here
            artist_tag = None

            scope_name = artist.split(":", maxsplit=1)
            if len(scope_name) > 1:
                artist_tag, tag_created = Tag.objects.get_or_create(
                    scope=scope_name[0],
                    name=scope_name[1])
            else:
                if Tag.objects.filter(scope="group", name=artist).first():
                    artist_tag = Tag.objects.filter(scope="group", name=artist).first()
                if Tag.objects.filter(scope="artist", name=artist).first():
                    artist_tag = Tag.objects.filter(scope="artist", name=artist).first()

                tag_created = False

            if not artist_tag:
                self.stdout.write("Skipping artist/group: {}, since it doesn't have a tag.".format(artist))
                continue

            if tag_created:
                self.stdout.write("Tag didn\'t exist and was created.")

            obj_raw, created = WantedGallery.objects.get_or_create(
                title='{} {} RAW: {}'.format(options['provider'].capitalize(), artist_tag.scope, artist_tag.name),
                book_type='user_{}_raw'.format(options['provider']).lower(),
                publisher=options['provider'].lower(),
                reason=options['provider'].lower(),
                defaults={
                    'should_search': options['should_search'],
                    'keep_searching': True,
                    'notify_when_found': False,
                    'public': False,
                    'wait_for_time': timedelta(minutes=45),
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
                obj_raw.categories.set(categories)
                for unwanted_tag in unwanted_tags:
                    obj_raw.unwanted_tags.add(unwanted_tag)
                if providers:
                    obj_raw.wanted_providers.set(providers)
                self.stdout.write("Added WantedGallery: {}".format(obj_raw.title))
            else:
                self.stdout.write("Skipping {}: {}, since it was already created.".format(artist_tag.scope, artist_tag.name))

            obj_english, created = WantedGallery.objects.get_or_create(
                title='{} {} English: {}'.format(options['provider'].capitalize(), artist_tag.scope, artist_tag.name),
                book_type='user_{}_eng'.format(options['provider']).lower(),
                publisher=options['provider'].lower(),
                reason=options['provider'].lower(),
                defaults={
                    'should_search': options['should_search'],
                    'keep_searching': True,
                    'notify_when_found': False,
                    'public': False,
                    'wait_for_time': timedelta(minutes=50),
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
                obj_english.categories.set(categories)

                for unwanted_tag in unwanted_tags:
                    obj_english.unwanted_tags.add(unwanted_tag)
                if providers:
                    obj_english.wanted_providers.set(providers)
                self.stdout.write("Added WantedGallery: {}".format(obj_english.title))
            else:
                self.stdout.write("Skipping {}: {}, since it was already created.".format(artist_tag.scope, artist_tag.name))

        end = time.perf_counter()

        self.stdout.write(
            self.style.SUCCESS(
                "Time taken (seconds, minutes): {0:.2f}, {1:.2f}".format(end - start, (end - start) / 60)
            )
        )
