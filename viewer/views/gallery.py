import typing
from django.contrib.auth.decorators import permission_required
from django.core.paginator import Paginator, Page, EmptyPage, InvalidPage
from django.http import HttpRequest, HttpResponse, Http404
from django.shortcuts import render


from viewer.models import (
    Gallery
)


@permission_required('viewer.read_gallery_change_log')
def change_log(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        gallery = Gallery.objects.get(pk=pk)
    except Gallery.DoesNotExist:
        raise Http404("Gallery does not exist")
    if not gallery.public and not request.user.is_authenticated:
        raise Http404("Gallery does not exist")

    gallery_history = gallery.history.order_by('-history_date')

    paginator = Paginator(gallery_history, 400)
    try:
        page = int(request.GET.get("page", '1'))
    except ValueError:
        page = 1

    try:
        results: typing.Optional[Page] = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    d = {'gallery': gallery, 'results': results}

    return render(request, "viewer/gallery_change_log.html", d)