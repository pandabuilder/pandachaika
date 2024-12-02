import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import permission_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from viewer.models import Archive, ItemProperties, Image
from viewer.services import CompareObjectsService
from viewer.utils.requests import double_check_auth

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)


def compare_archives(request: HttpRequest) -> HttpResponse:

    authenticated, actual_user = double_check_auth(request)

    if not actual_user or not actual_user.has_perm('viewer.download_gallery'):
        algos = request.GET.getlist("algos", ['phash'])
        thumbs = False
        no_images = False
        archive_pks: list[str] = request.GET.getlist("pk", [])
        no_live_data = True
        archives = Archive.objects.filter(pk__in=archive_pks, public=True)
    else:
        algos = request.GET.getlist("algos", ['phash'])
        thumbs = bool(request.GET.get("thumbs", False))
        no_images = bool(request.GET.get("no-imgs", False))
        archive_pks = request.GET.getlist("pk", [])
        no_live_data = False
        archives = Archive.objects.filter(pk__in=archive_pks)
    """Archive compare."""
    response: dict = {}

    results = CompareObjectsService.hash_archives(
        archives, algos, thumbnails=thumbs, images=not no_images, item_model=ItemProperties, image_model=Image,
        no_live_data=no_live_data
    )

    response['results'] = results

    return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")


def compare_archives_viewer(request: HttpRequest) -> HttpResponse:
    authenticated, actual_user = double_check_auth(request)
    if authenticated:
        return render(request, "viewer/collaborators/compare_archives.html")
    else:
        return render(request, "viewer/collaborators/compare_archives_public.html")

@permission_required('viewer.change_archivegroup', raise_exception=True)
def archive_group_editor(request: HttpRequest) -> HttpResponse:

    return render(request, "viewer/collaborators/archive_group_editor.html")
