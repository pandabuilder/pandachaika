import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import permission_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from viewer.models import Archive, ItemProperties, Image
from viewer.services import CompareObjectsService

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)


@permission_required('viewer.compare_archives')
def compare_archives(request: HttpRequest) -> HttpResponse:
    response: dict = {}
    algos = request.GET.getlist("algos", ['phash'])
    thumbs = bool(request.GET.get("thumbs", False))
    no_imgs = request.GET.getlist("no-imgs", False)
    archive_pks: list[str] = request.GET.getlist("pk", [])
    """Archive compare."""

    archives = Archive.objects.filter(pk__in=archive_pks)

    results = CompareObjectsService.hash_archives(
        archives, algos, thumbnails=thumbs, images=not no_imgs, item_model=ItemProperties, image_model=Image
    )

    response['results'] = results

    return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")


@permission_required('viewer.compare_archives')
def compare_archives_viewer(request: HttpRequest) -> HttpResponse:

    return render(request, "viewer/collaborators/compare_archives.html")
