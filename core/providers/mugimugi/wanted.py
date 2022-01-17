import datetime
import logging
import typing
import urllib.parse

import django.utils.timezone as django_tz

from core.base.types import DataDict
from core.base.utilities import request_with_retries, format_title_to_wanted_search, construct_request_dict
from core.providers.mugimugi.utilities import convert_api_response_text_to_gallery_dicts
from viewer.models import Gallery, WantedGallery, Provider, Artist
from . import constants

if typing.TYPE_CHECKING:
    from core.base.setup import Settings
    from viewer.models import AttributeManager

logger = logging.getLogger(__name__)


def wanted_generator(settings: 'Settings', attrs: 'AttributeManager'):
    own_settings = settings.providers[constants.provider_name]

    if not own_settings.api_key:
        logger.error("Can't use {} API without an api key. Check {}/API_MANUAL.txt".format(
            constants.provider_name,
            constants.main_page
        ))
        return False

    queries: DataDict = {}
    queries_slist_params: DataDict = {}

    for attr in attrs.filter(name__startswith='wanted_params_'):

        attr_info = attr.name.replace('wanted_params_', '')
        query_name, attr_name = attr_info.split("_", maxsplit=1)

        if query_name not in queries:
            queries[query_name] = {
                'page': 1,
                'S': 'objectSearch',
                'match': 0,
                'order': 'added',
                'flow': 'DESC'
            }

        if attr_name.startswith('slist_'):
            if query_name not in queries_slist_params:
                queries_slist_params[query_name] = []
            queries_slist_params[query_name].append('{}:{}'.format(attr_name.replace('slist_', ''), attr.value))
        else:
            queries[query_name].update({attr_name: attr.value})

    for query_name, slist_params in queries_slist_params.items():
        queries[query_name].update({'slist': '|'.join(slist_params)})

    for query_name, query_values in queries.items():

        while True:
            # Read the values from the newly created Provider Model,
            # that should be created like this (extracted from from):
            # wanted_params_match: Any, Sounds Like, Start With, End With, Exact -> 0, 4, 1, 2, 3
            # wanted_params_age: 18+ -> blank/Y/N
            # wanted_params_anth: Anthology -> blank/Y/N
            # wanted_params_bcopy: Copybook -> blank/Y/N
            # wanted_params_FREE: Free -> blank/Y/N
            # wanted_params_flist: Type ->
            # blank: Any
            # 19: Bootleg
            # 18: Calendar
            # 12: Commercial Artbook
            # 8: Commercial CG
            # 7: Commercial Magazine
            # 25: Commercial Mook
            # 11: Commercial Novel
            # 10: Commercial other
            # 13: Commercial other book
            # 9: Commercial Soft
            # 2: Doujin CG
            # 24: Doujin Goods
            # 23: Doujin Movie
            # 22: Doujin Music
            # 21: Doujin Novel
            # 4: Doujin Other
            # 3: Doujin Soft
            # 1: Doujinshi
            # 5: Manga
            # 6: Manga (Part)
            # 17: Postcard
            # 16: Poster
            # 15: Shitajiki
            # 14: Telephone Card
            # 20: Unknown
            # wanted_params_date: Release date from -> yyyy-mm-dd
            # wanted_params_date2: Release date to -> yyyy-mm-dd
            # for slist parameters:
            # Here is the list of ALL search terms:
            # C: Circle
            # A: Author
            # P: Parody
            # H: Character
            # N: Convention
            # O: Collections
            # K: Content
            # G: Genre
            # T: Type
            # L: Publisher
            # I: Imprint
            # wanted_params_slist_C: Separated by |
            # wanted_params_slist_A: Separated by |
            # wanted_params_slist_P: Separated by |
            # wanted_params_slist_H: Separated by |
            # wanted_params_slist_K: Separated by |
            # wanted_params_slist_G: Separated by |
            # wanted_params_slist_N: One
            # wanted_params_slist_O: One
            # wanted_params_slist_L: One
            # wanted_params_slist_I: One
            # wanted_params_cont: One
            # wanted_params_sub: One
            # wanted_params_scen: Censored -> blank/Y/N

            new_query = urllib.parse.urlencode(query_values, doseq=True)

            logger.info('Querying {} for auto wanted galleries, page: {}, query name: {}, query: {}'.format(
                constants.provider_name, query_values['page'], query_name, new_query)
            )

            link = '{}/api/{}/?{}'.format(
                constants.main_page,
                own_settings.api_key,
                new_query
            )

            provider, provider_created = Provider.objects.get_or_create(
                slug=constants.provider_name, defaults={'name': constants.provider_name}
            )

            remaining_queries, int_created = attrs.get_or_create(
                provider=provider,
                name='remaining_queries',
                data_type='int',
                defaults={
                    'value_int': constants.daily_requests,
                }
            )

            last_query_date, date_created = attrs.get_or_create(
                provider=provider,
                name='last_query_date',
                data_type='date',
                defaults={
                    'value_date': django_tz.now(),
                }

            )

            if not date_created:
                limit_time = datetime.time(tzinfo=datetime.timezone(datetime.timedelta(hours=1)))  # GMT+1 is when server resets
                if last_query_date.value.timetz() < limit_time < django_tz.now().timetz():
                    remaining_queries.value = constants.daily_requests
                    remaining_queries.save()

            if remaining_queries.value <= 0:
                logger.warning("Daily queries quota {} reached for {}. It resets at 00:00 GMT+1".format(
                    constants.daily_requests,
                    constants.provider_name
                ))
                return

            request_dict = construct_request_dict(settings, own_settings)

            response = request_with_retries(
                link,
                request_dict,
                post=False,
            )

            remaining_queries.value -= 1
            remaining_queries.save()
            last_query_date.value = django_tz.now()
            last_query_date.save()

            if not response:
                logger.error(
                    'For provider {}: Got to page {}, but did not get a response, stopping'.format(
                        constants.provider_name,
                        query_values['page']
                    )
                )
                break

            response.encoding = 'utf-8'
            # Based on: https://www.doujinshi.org/API_MANUAL.txt

            api_galleries = convert_api_response_text_to_gallery_dicts(response.text)

            if not api_galleries:
                logger.error('Server response: {}'.format(response.text))
                logger.error(
                    'For provider {}: Got to page {}, but could not parse the response into galleries, stopping'.format(
                        constants.provider_name,
                        query_values['page']
                    )
                )
                break

            # Listen to what the server says
            remaining_queries.value = api_galleries[0].queries
            remaining_queries.save()

            used = Gallery.objects.filter(gid__in=[x.gid for x in api_galleries], provider=constants.provider_name)

            # If the amount of galleries present in database is equal to what we get from the page,
            # we assume we already processed everything. You can force to process everything by using:
            force_process, force_created = attrs.get_or_create(
                provider=provider,
                name='force_process',
                data_type='bool',
                defaults={
                    'value_bool': False,
                }
            )

            logger.info(
                'For provider {}: Page has {} galleries, from which {} are already present in the database.'.format(
                    constants.provider_name,
                    len(api_galleries),
                    used.count()
                )
            )

            if not force_process.value and used.count() == len(api_galleries):
                logger.info(
                    'For provider {}: Got to page {}, it has already been processed entirely, stopping'.format(
                        constants.provider_name,
                        query_values['page']
                    )
                )
                break

            used_gids = used.values_list('gid', flat=True)

            for gallery_data in api_galleries:
                if gallery_data.gid not in used_gids:
                    if not gallery_data.dl_type:
                        gallery_data.dl_type = 'auto_wanted'
                    wanted_reason = attrs.fetch_value('wanted_reason_{}'.format(query_name))
                    if isinstance(wanted_reason, str):
                        gallery_data.reason = wanted_reason or 'backup'
                    gallery = Gallery.objects.add_from_values(gallery_data)
                    # We match anyways in case there's a previous WantedGallery.
                    # Actually, we don't match since we only get metadata here, so it should not count as found.
                    publisher_name = ''
                    publisher = gallery.tags.filter(scope='publisher').first()
                    if publisher:
                        publisher_name = publisher.name

                    if not gallery.title_jpn:
                        continue

                    search_title = format_title_to_wanted_search(gallery.title_jpn)

                    wanted_galleries: typing.Iterable[WantedGallery] = WantedGallery.objects.filter(
                        title_jpn=gallery.title_jpn, search_title=search_title
                    )

                    if not wanted_galleries:
                        wanted_gallery = WantedGallery.objects.create(
                            title=gallery.title or gallery.title_jpn,
                            title_jpn=gallery.title_jpn,
                            search_title=search_title,
                            book_type=gallery.category,
                            page_count=gallery.filecount,
                            publisher=publisher_name,
                            reason=attrs.fetch_value('wanted_reason_{}'.format(query_name)) or '',
                            public=attrs.fetch_value('wanted_public_{}'.format(query_name)) or False,
                            should_search=attrs.fetch_value('wanted_should_search_{}'.format(query_name)) or True,
                            keep_searching=attrs.fetch_value('wanted_keep_searching_{}'.format(query_name)) or True,
                            category='Manga',
                            unwanted_title=own_settings.unwanted_title or settings.auto_wanted.unwanted_title
                        )
                        wanted_provider_string = attrs.fetch_value('wanted_provider_{}'.format(query_name))
                        if wanted_provider_string and isinstance(wanted_provider_string, str):
                            wanted_provider_instance = Provider.objects.filter(slug=wanted_provider_string).first()
                            if wanted_provider_instance:
                                wanted_gallery.wanted_providers.add(wanted_provider_instance)
                        wanted_providers_string = attrs.fetch_value('wanted_providers_{}'.format(query_name))
                        if wanted_providers_string and isinstance(wanted_providers_string, str):
                            for wanted_provider in wanted_providers_string.split():
                                wanted_provider = wanted_provider.strip()
                                wanted_provider_instance = Provider.objects.filter(slug=wanted_provider).first()
                                if wanted_provider_instance:
                                    wanted_gallery.wanted_providers.add(wanted_provider_instance)

                        for artist in gallery.tags.filter(scope='artist'):
                            artist_obj = Artist.objects.filter(name_jpn=artist.name).first()
                            if not artist_obj:
                                artist_obj = Artist.objects.create(name=artist.name, name_jpn=artist.name)
                            wanted_gallery.artists.add(artist_obj)
                        logger.info(
                            "Created wanted gallery ({}): {}, search title: {}".format(
                                wanted_gallery.book_type,
                                wanted_gallery.get_absolute_url(),
                                gallery.title_jpn
                            )
                        )

                        wanted_galleries = [wanted_gallery]

                    for wanted_gallery in wanted_galleries:

                        mention, mention_created = wanted_gallery.mentions.get_or_create(
                            mention_date=gallery.create_date,
                            release_date=gallery.posted,
                            type='release_date',
                            source=constants.provider_name,
                        )
                        if mention_created and gallery.thumbnail:
                            mention.copy_img(gallery.thumbnail.path)
                            wanted_gallery.calculate_nearest_release_date()

            # galleries.extend(api_galleries)

            # API returns 25 max results per query, so if we get 24 or less, means there's no more pages.
            # API Manual says 25, but we get 50 results normally!
            if len(api_galleries) < 50:
                logger.info(
                    'Got to page {}, and we got less than 50 galleries, '
                    'meaning there is no more pages, stopping'.format(query_values['page'])
                )
                break

            query_values['page'] += 1

    logger.info("{} Auto wanted ended.".format(
        constants.provider_name
    ))
