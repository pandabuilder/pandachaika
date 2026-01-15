import logging
from typing import Any

from django.shortcuts import render
from django.http import HttpRequest, HttpResponse
from django.contrib.contenttypes.models import ContentType

from viewer.models import ItemProperties, Image
from viewer.services import CompareObjectsService
from viewer.utils.requests import double_check_auth

logger = logging.getLogger(__name__)

def image_search(request: HttpRequest) -> HttpResponse:
    authenticated, actual_user = double_check_auth(request)

    context: dict[str, Any] = {}
    
    if request.method == "POST":
        uploaded_file = request.FILES.get('image')

        if uploaded_file:
            if uploaded_file.content_type and uploaded_file.content_type.startswith("image/"):
                try:
                    target_phash_obj = CompareObjectsService.hash_thumbnail(uploaded_file, 'phash')
                    target_phash_str = str(target_phash_obj)

                    context['target_phash'] = target_phash_str

                    image_ct = ContentType.objects.get_for_model(Image)

                    properties = ItemProperties.objects.filter(tag="hash-compare", name="phash", value=target_phash_str, content_type=image_ct)

                    final_results = []
                    for prop in properties:
                        image_obj = prop.content_object
                        if image_obj is not None:
                            archive = image_obj.archive
                            if archive.public:
                                final_results.append({
                                    'archive': archive,
                                    'image': image_obj
                                })
                            elif authenticated:
                                final_results.append({
                                    'archive': archive,
                                    'image': image_obj
                                })

                    context['results'] = final_results

                except Exception as e:
                    logger.error(f"Error in image search: {e}")
                    context['error'] = str(e)
            else:
                error = f"Uploaded file is not an image: {uploaded_file.content_type}"
                logger.error(error)
                context['error'] = error
                
        if request.headers.get('HX-Request') == 'true':
            return render(request, "viewer/include/image_search_results.html", context)
                
    return render(request, "viewer/image_search.html", context)

