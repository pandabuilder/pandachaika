from django.core.paginator import Paginator, Page, EmptyPage, InvalidPage
from django.http import HttpRequest, HttpResponse, Http404
from django.shortcuts import render


from viewer.models import Gallery, GalleryChange
from viewer.models.change_log import GALLERY_PUBLIC_FIELDS, GALLERY_TRACKED_FIELDS


CHANGE_LOG_PAGE_SIZE = 200


def change_log(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        gallery = Gallery.objects.get(pk=pk)
    except Gallery.DoesNotExist:
        raise Http404("Gallery does not exist")
    if not gallery.public and not request.user.is_authenticated:
        raise Http404("Gallery does not exist")

    fields = GALLERY_TRACKED_FIELDS if request.user.is_authenticated else GALLERY_PUBLIC_FIELDS
    entries = gallery.change_log.all()
    if not request.user.is_authenticated:
        entries = entries.filter(GalleryChange.public_filter())

    paginator = Paginator(entries, CHANGE_LOG_PAGE_SIZE)
    try:
        page = int(request.GET.get("page", "1"))
    except ValueError:
        page = 1

    try:
        results: Page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    rows = [(entry, entry.field_changes(fields)) for entry in results.object_list]

    d = {"gallery": gallery, "results": results, "rows": rows, "tracked_fields": fields}

    return render(request, "viewer/gallery_change_log.html", d)
