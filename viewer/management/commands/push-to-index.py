from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.paginator import Paginator

from viewer.models import Archive, Gallery
from viewer.services.elasticsearch_index import ElasticsearchIndexService
from viewer.utils.elasticsearch import ES_MATCH_MAPPING

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = "Recreate index and push data in bulk."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.es_client = ElasticsearchIndexService.create_client()

    def add_arguments(self, parser):
        parser.add_argument(
            "-r",
            "--recreate_index",
            required=False,
            action="store_true",
            default=False,
            help="Creates index and puts mapping in it.",
        )
        parser.add_argument(
            "-p",
            "--push_to_index",
            required=False,
            action="store_true",
            default=False,
            help="Calls converter and uploads data to the index.",
        )
        parser.add_argument(
            "-rg",
            "--recreate_index_gallery",
            required=False,
            action="store_true",
            default=False,
            help="Creates index and puts mapping in it (Gallery).",
        )
        parser.add_argument(
            "-pg",
            "--push_to_index_gallery",
            required=False,
            action="store_true",
            default=False,
            help="Calls converter and uploads data to the index (Gallery).",
        )
        parser.add_argument(
            "-bs",
            "--bulk_size",
            required=False,
            action="store",
            type=int,
            help="Specify bulk size when adding to index.",
        )
        parser.add_argument(
            "-rm",
            "--recreate_match_index",
            required=False,
            action="store_true",
            default=False,
            help="Creates the match index and puts mapping in it.",
        )

    def handle(self, *args, **options):

        if options["recreate_match_index"]:
            self.recreate_match_index_model()

        if options["recreate_index"]:
            self.recreate_index_model(Archive)
        if options["recreate_index_gallery"]:
            self.recreate_index_model(Gallery)
        if options["push_to_index"]:
            self.push_db_to_index_model(Archive, options["bulk_size"])
        if options["push_to_index_gallery"]:
            self.push_db_to_index_model(Gallery, options["bulk_size"])

    def recreate_match_index_model(self):
        ElasticsearchIndexService.recreate_index(
            crawler_settings.elasticsearch.match_index_name,
            ES_MATCH_MAPPING,
            client=self.es_client,
        )

    def recreate_index_model(self, model: type[Gallery] | type[Archive]):
        ElasticsearchIndexService.recreate_model_index(model, client=self.es_client)

    def push_db_to_index_model(self, model: type[Gallery] | type[Archive], bulk_size: int = 0):
        query = ElasticsearchIndexService.get_queryset_for_indexing(model, order_by_pk_desc=True)

        if not bulk_size:
            ElasticsearchIndexService.bulk_index(
                query,
                op_type="create",
                doc_as_upsert=False,
                client=self.es_client,
                force=True,
                respect_public_only=False,
            )
        else:
            paginator = Paginator(query, bulk_size)

            for page_number in paginator.page_range:
                self.stdout.write("Bulk sending page {} of {}.".format(page_number, paginator.num_pages))
                page = paginator.page(page_number)
                ElasticsearchIndexService.bulk_index(
                    page,
                    op_type="create",
                    doc_as_upsert=False,
                    chunk_size=bulk_size,
                    client=self.es_client,
                    force=True,
                    respect_public_only=False,
                )

    def convert_for_bulk(self, django_object, action=None):
        return ElasticsearchIndexService.to_bulk_action(
            django_object,
            op_type=action or "index",
            doc_as_upsert=False,
        )
