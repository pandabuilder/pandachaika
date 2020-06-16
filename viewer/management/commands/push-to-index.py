from django.conf import settings
from django.core.management.base import BaseCommand
from viewer.models import Archive


class Command(BaseCommand):
    help = "Recreate index and push data in bulk."

    def add_arguments(self, parser):
        parser.add_argument('-r', '--recreate_index',
                            required=False,
                            action='store_true',
                            default=False,
                            help='Creates index and puts mapping in it.')
        parser.add_argument('-p', '--push_to_index',
                            required=False,
                            action='store_true',
                            default=False,
                            help='Calls converter and uploads data to the index.')

    def handle(self, *args, **options):
        if options['recreate_index']:
            self.recreate_index()
        if options['push_to_index']:
            self.push_db_to_index()

    def recreate_index(self):

        from elasticsearch.client import IndicesClient

        indices_client = IndicesClient(client=settings.ES_CLIENT)
        index_name = Archive._meta.es_index_name
        if indices_client.exists(index_name):
            indices_client.delete(index=index_name)
        indices_client.create(index=index_name)
        indices_client.close(index=index_name)
        indices_client.put_settings(
            index=index_name,
            body={
                "index": {"max_result_window": settings.MAX_RESULT_WINDOW},
                "analysis": {
                    "filter": {
                        "edge_ngram_filter": {
                            "type": "edge_ngram",
                            "min_gram": 2,
                            "max_gram": 20
                        }
                    },
                    "analyzer": {
                        "edge_ngram_analyzer": {
                            "type": "custom",
                            "tokenizer": "standard",
                            "filter": [
                                "lowercase",
                                "edge_ngram_filter"
                            ]
                        }
                    }
                }
            }
        )
        indices_client.put_mapping(
            body=Archive._meta.es_mapping,
            index=index_name,
            # doc_type=Archive._meta.es_type_name
        )
        indices_client.open(index=index_name)

    def push_db_to_index(self):

        from elasticsearch.helpers import bulk

        data = [
            self.convert_for_bulk(s, 'create') for s in Archive.objects.all()
        ]
        bulk(client=settings.ES_CLIENT, actions=data, stats_only=True)

    def convert_for_bulk(self, django_object, action=None):
        data = django_object.es_repr()
        metadata = {
            '_op_type': action,
            "_index": django_object._meta.es_index_name,
            # "_type": django_object._meta.es_type_name,
        }
        data.update(**metadata)
        return data
