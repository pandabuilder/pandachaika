import json
import os

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpRequest
from django.conf import settings

from viewer.utils.dirbrowser import DirBrowser


@login_required
def directory_parser(request: HttpRequest) -> HttpResponse:
    if not request.user.is_staff:
        return HttpResponse(json.dumps({"Error": "You need to be an admin crawl a directory."}), content_type="application/json; charset=utf-8")
    p = request.GET
    directory = ''
    if p and 'dir' in p:
        directory = p['dir']
    fs = DirBrowser(settings.MEDIA_ROOT)
    if fs.isdir(directory):
        files = fs.files(directory)
    else:
        files = fs.files(os.path.dirname(directory))
    return HttpResponse(json.dumps((fs.relativepath(directory), files)), content_type="application/json; charset=utf-8")
